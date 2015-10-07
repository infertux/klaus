"""A cache for tagsfiles generated by the 'ctags' command line tool.

We don't want to run the 'ctags' command line tool on each request as it may
take a lot of time.  The following steps are necessary in order to create a
ctags tagsfile that be read by Pygments:

1. Clone the repository to a temporary location and check out the branch/commit
   the user is browsing, unless the branch is already checked out. (*)
2. Run 'ctags -R' on the temporary repository checkout.
3. Delete the temporary repository checkout.

To avoid going through these steps on each request, we cache the tagsfile
generated in step 2.  The cache is on-disk and non-persistent, i.e. cleared
whenever the Python interpreter running klaus is shut down.

For large projects, the ctags tagsfiles may grow to sizes of multiple MiB, so
we have to set an upper limit on the size of the cache.  Since tagsfiles are
represented as uncompressed ASCII files, we can increase the number of tagsfiles
we can cache by using compression.  Of course, 'python-ctags', which is used by
Pygments to read the tagsfiles, can't deal with compressed tagsfiles, so we have
to uncompress them before actually using them. To avoid decompressing tagsfiles
on each request, we keep the tagsfiles that are most likely to be used (**) in
uncompressed form.

(*) We always create a clone in the current implementation;
    this could be optimized in the future.
(**) "most likely": currently implemented as "most recently used"
"""
import os
import shutil
import tempfile
import threading
import gzip
from dulwich.lru_cache import LRUSizeCache
from klaus.ctags import create_tagsfile, delete_tagsfile


# Good compression while taking only 10% more time than level 1
COMPRESSION_LEVEL = 4


def compress_tagsfile(uncompressed_tagsfile_path):
    """Compress an uncompressed tagsfile.

    :return: path to the compressed version of the tagsfile
    """
    _, compressed_tagsfile_path = tempfile.mkstemp()
    with open(uncompressed_tagsfile_path, 'rb') as uncompressed:
        with gzip.open(compressed_tagsfile_path, 'wb', COMPRESSION_LEVEL) as compressed:
            shutil.copyfileobj(uncompressed, compressed)
    return compressed_tagsfile_path


def uncompress_tagsfile(compressed_tagsfile_path):
    """Uncompress an compressed tagsfile.

    :return: path to the uncompressed version of the tagsfile
    """
    _, uncompressed_tagsfile_path = tempfile.mkstemp()
    with gzip.open(compressed_tagsfile_path, 'rb') as compressed:
        with open(uncompressed_tagsfile_path, 'wb') as uncompressed:
            shutil.copyfileobj(compressed, uncompressed)
    return uncompressed_tagsfile_path


MiB = 1024 * 1024

class CTagsCache(object):
    """A ctags cache. Both uncompressed and compressed entries are kept in
    temporary files created by `tempfile.mkstemp` which are deleted from disk
    when the Python interpreter is shut down.

    :param uncompressed_max_bytes: Maximum size of the uncompressed cache sector
    :param compressed_max_bytes:   Maximum size of the compressed cache sector

    The lifecycle of a cache entry is as follows.

    - When first created, a tagsfile is put into the uncompressed cache sector.
    - When free space is required for other uncompressed tagsfiles, it may be
      moved to the compressed cache sector. Gzip is used to compress the tagsfile.
    - When free space is required for other compressed tagsfiles, it may be
      evicted from the cache entirely.
    - When the tagsfile is requested and it's in the compressed cache sector,
      it is moved back to the uncompressed sector prior to using it.
    """
    def __init__(self, uncompressed_max_bytes=30*MiB, compressed_max_bytes=20*MiB):
        self.uncompressed_max_bytes = uncompressed_max_bytes
        self.compressed_max_bytes = compressed_max_bytes
        # Note: We use dulwich's LRU cache to store the tagsfile paths here,
        # but we could easily replace it by any other (LRU) cache implementation.
        self._uncompressed_cache = LRUSizeCache(uncompressed_max_bytes, compute_size=os.path.getsize)
        self._compressed_cache   = LRUSizeCache(compressed_max_bytes,   compute_size=os.path.getsize)
        self._clearing = False
        self._lock = threading.Lock()

    def __del__(self):
        self.clear()

    def clear(self):
        """Clear both the uncompressed and compressed caches."""
        # Don't waste time moving tagsfiles from uncompressed to compressed cache,
        # but remove them directly instead:
        self._clearing = True
        self._uncompressed_cache.clear()
        self._compressed_cache.clear()
        self._clearing = False

    def get_tagsfile(self, git_repo_path, git_rev):
        """Get the ctags tagsfile for the given Git repository and revision.

        - If the tagsfile is still in cache, and in uncompressed form, return it
          without any further cost.
        - If the tagsfile is still in cache, but in compressed form, uncompress
          it, put it into uncompressed space, and return the uncompressed version.
        - If the tagsfile isn't in cache at all, create it, put it into
          uncompressed cache and return the newly created version.
        """
        # Always require full SHAs
        assert len(git_rev) == 40

        # Avoiding race conditions, The Sledgehammer Way
        with self._lock:
            if git_rev in self._uncompressed_cache:
                return self._uncompressed_cache[git_rev]

            if git_rev in self._compressed_cache:
                compressed_tagsfile_path = self._compressed_cache[git_rev]
                uncompressed_tagsfile_path = uncompress_tagsfile(compressed_tagsfile_path)
                self._compressed_cache._remove_node(self._compressed_cache._cache[git_rev])
            else:
                # Not in cache.
                uncompressed_tagsfile_path = create_tagsfile(git_repo_path, git_rev)
            self._uncompressed_cache.add(git_rev, uncompressed_tagsfile_path,
                                         self._clear_uncompressed_entry)
            return uncompressed_tagsfile_path

    def _clear_uncompressed_entry(self, git_rev, uncompressed_tagsfile_path):
        """Called by LRUSizeCache whenever an entry is to be evicted from
        uncompressed cache.

        Most of the times this happens when space is needed
        in uncompressed cache, in which case we move the tagsfile to compressed
        cache.  When clearing the cache, we don't bother moving entries to
        uncompressed space; we delete them directly instead.
        """
        if not self._clearing:
            # If we're clearing the whole cache, don't waste time moving tagsfiles
            # from uncompressed to compressed cache, but remove them directly instead.
            self._compressed_cache.add(git_rev, compress_tagsfile(uncompressed_tagsfile_path),
                                       self._clear_compressed_entry)
        delete_tagsfile(uncompressed_tagsfile_path)

    def _clear_compressed_entry(self, git_rev, compressed_tagsfile_path):
        """Called by LRUSizeCache whenever an entry to be evicted from
        compressed cache.

        This happens when space is needed for new compressed
        tagsfiles.  We delete the evictee from the cache entirely.
        """
        delete_tagsfile(compressed_tagsfile_path)
