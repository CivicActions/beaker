import datetime
import os
import threading
import time

try:
    import pymongo
    import pymongo.errors
except ImportError:
    pymongo = None

from beaker.container import NamespaceManager
from beaker.synchronization import SynchronizerImpl
from beaker.util import SyncDict
from beaker._compat import string_type


MONGO_CLIENTS = {}


class MongoNamespaceManager(NamespaceManager):
    """Provides the :class:`.NamespaceManager` API over a memcache client library."""

    clients = SyncDict()

    def __init__(self, namespace, url, **kw):
        super(MongoNamespaceManager, self).__init__(namespace)
        self.lock_dir = None  # MongoDB uses mongo itself for locking.

        if isinstance(url, string_type):
            self.client = pymongo.MongoClient(url)
        else:
            self.client = url
        self.db = self.client.get_default_database()

    def _format_key(self, key):
        return '%s:%s' % (self.namespace, key)

    def get_creation_lock(self, key):
        return MongoSynchronizer(self._format_key(key), self.client)

    def __getitem__(self, key):
        entry = self.db.backer_cache.find_one({'_id': self._format_key(key)})
        if entry is None:
            raise KeyError(key)
        return entry['value']

    def __contains__(self, key):
        entry = self.db.backer_cache.find_one({'_id': self._format_key(key)})
        return entry is not None

    def has_key(self, key):
        return key in self

    def __setitem__(self, key, value):
        self.db.backer_cache.update_one({'_id': self._format_key(key)},
                                        {'$set': {'value': value}},
                                        upsert=True)

    def __delitem__(self, key):
        self.db.backer_cache.delete_many({'_id': self._format_key(key)})

    def do_remove(self):
        self.db.backer_cache.delete_many({'_id': {'$regex': '^%s' % self.namespace}})

    def keys(self):
        return [e['key'].split(':', 1)[-1] for e in self.db.backer_cache.find_all(
            {'_id': {'$regex': '^%s' % self.namespace}}
        )]


class MongoSynchronizer(SynchronizerImpl):
    # If a cache entry generation function can take a lot,
    # but 15 minutes is more than a reasonable time.
    LOCK_EXPIRATION = 900

    def __init__(self, identifier, url):
        super(MongoSynchronizer, self).__init__()
        self.identifier = identifier
        if isinstance(url, string_type):
            self.client = pymongo.MongoClient(url)
        else:
            self.client = url
        self.db = self.client.get_default_database()

    def _clear_expired_locks(self):
        now = datetime.datetime.utcnow()
        expired = now - datetime.timedelta(seconds=self.LOCK_EXPIRATION)
        self.db.beaker_locks.delete_many({'_id': self.identifier, 'timestamp': {'$lte': expired}})
        return now

    def _get_owner_id(self):
        return '%s-%s' % (os.getpid(), threading.current_thread().ident)

    def do_release_read_lock(self):
        self.db.beaker_locks.update_one({'_id': self.identifier, 'readers': self._get_owner_id()},
                                        {'$pull': {'readers': self._get_owner_id()}})

    def do_acquire_read_lock(self, wait):
        now = self._clear_expired_locks()
        while True:
            try:
                self.db.beaker_locks.update_one({'_id': self.identifier, 'owner': None},
                                                {'$set': {'timestamp': now},
                                                 '$push': {'readers': self._get_owner_id()}},
                                                upsert=True)
                return True
            except pymongo.errors.DuplicateKeyError:
                if not wait:
                    return False
                time.sleep(0.2)

    def do_release_write_lock(self):
        self.db.beaker_locks.delete_one({'_id': self.identifier, 'owner': self._get_owner_id()})

    def do_acquire_write_lock(self, wait):
        now = self._clear_expired_locks()
        while True:
            try:
                self.db.beaker_locks.update_one({'_id': self.identifier, 'owner': None,
                                                 'readers': []},
                                                {'$set': {'owner': self._get_owner_id(),
                                                          'timestamp': now}},
                                                upsert=True)
            except pymongo.errors.DuplicateKeyError:
                if not wait:
                    return False
                time.sleep(0.2)
