import ast
import collections
import dask
import dask.bag
from datetime import datetime
import event_model
from functools import partial
import importlib
import intake
import intake.catalog
import intake.catalog.base
import intake.catalog.local
import intake.catalog.remote
import intake.container.base
import intake.container.semistructured
import intake.source.base
from intake.compat import unpack_kwargs
import intake_xarray.base
import itertools
import msgpack
import numpy
import pandas
import pymongo
import pymongo.errors
import requests
from requests.compat import urljoin, urlparse
import time
import xarray

from .core import RemoteRunCatalog, documents_to_xarray


class FacilityCatalog(intake.catalog.Catalog):
    "spans multiple MongoDB instances"
    ...


def parse_handler_registry(handler_registry):
    result = {}
    for spec, handler_str in handler_registry.items():
        module_name, _, class_name = handler_str.rpartition('.')
        result[spec] = getattr(importlib.import_module(module_name), class_name)
    return result


class MongoMetadataStoreCatalog(intake.catalog.Catalog):
    def __init__(self, metadatastore_uri, asset_registry_uri, *,
                 handler_registry=None, query=None, **kwargs):
        """
        Insert documents into MongoDB using layout v1.

        This layout uses a separate Mongo collection per document type and a
        separate Mongo document for each logical document.

        Note that this Seralizer does not share the standard Serializer
        name or signature common to suitcase packages because it can only write
        via pymongo, not to an arbitrary user-provided buffer.
        """
        name = 'mongo_metadatastore'

        self._metadatastore_uri = metadatastore_uri
        self._asset_registry_uri = asset_registry_uri
        metadatastore_client = pymongo.MongoClient(metadatastore_uri)
        asset_registry_client = pymongo.MongoClient(asset_registry_uri)
        self._metadatastore_client = metadatastore_client
        self._asset_registry_client = asset_registry_client

        try:
            # Called with no args, get_database() returns the database
            # specified in the client's uri --- or raises if there was none.
            # There is no public method for checking this in advance, so we
            # just catch the error.
            mds_db = self._metadatastore_client.get_database()
        except pymongo.errors.ConfigurationError as err:
            raise ValueError(
                f"Invalid metadatastore_client: {metadatastore_client} "
                f"Did you forget to include a database?") from err
        try:
            assets_db = self._asset_registry_client.get_database()
        except pymongo.errors.ConfigurationError as err:
            raise ValueError(
                f"Invalid asset_registry_client: {asset_registry_client} "
                f"Did you forget to include a database?") from err

        self._run_start_collection = mds_db.get_collection('run_start')
        self._run_stop_collection = mds_db.get_collection('run_stop')
        self._event_descriptor_collection = mds_db.get_collection('event_descriptor')
        self._event_collection = mds_db.get_collection('event')

        self._resource_collection = assets_db.get_collection('resource')
        self._datum_collection = assets_db.get_collection('datum')

        self._query = query or {}
        if handler_registry is None:
            handler_registry = {}
        parsed_handler_registry = parse_handler_registry(handler_registry)
        self.filler = event_model.Filler(parsed_handler_registry)
        super().__init__(**kwargs)

    def _get_run_stop(self, run_start_uid):
        doc = self._run_stop_collection.find_one(
            {'run_start': run_start_uid})
        # It is acceptable to return None if the document does not exist.
        if doc is not None:
            doc.pop('_id')
        return doc

    def _get_event_descriptors(self, run_start_uid):
        results = []
        cursor = self._event_descriptor_collection.find(
            {'run_start': run_start_uid},
            sort=[('time', pymongo.ASCENDING)])
        for doc in cursor:
            doc.pop('_id')
            results.append(doc)
        return results

    def _get_event_cursor(self, descriptor_uids, skip=0, limit=None):
        cursor = (self._event_collection
            .find({'descriptor': {'$in': descriptor_uids}},
                    sort=[('time', pymongo.ASCENDING)]))
        cursor.skip(skip)
        if limit is not None:
            cursor = cursor.limit(limit)
        for doc in cursor:
            doc.pop('_id')
            yield doc

    def _get_event_count(self, descriptor_uids):
        cursor = (self._event_collection
            .find({'descriptor': {'$in': descriptor_uids}},
                    sort=[('time', pymongo.ASCENDING)]))
        return cursor.count()

    def _get_resource(self, uid):
        doc = self._resource_collection.find_one(
            {'uid': uid})
        if doc is None:
            raise ValueError(f"Could not find Resource with uid={uid}")
        doc.pop('_id')
        return doc

    def _get_datum(self, datum_id):
        doc = self._datum_collection.find_one(
            {'datum_id': datum_id})
        if doc is None:
            raise ValueError(f"Could not find Datum with datum_id={datum_id}")
        doc.pop('_id')
        return doc

    def _get_datum_cursor(self, resource_uid):
        cursor = self._datum_collection.find({'resource': resource_uid})
        for doc in cursor:
            doc.pop('_id')
            yield doc

        self._schema = {}  # TODO This is cheating, I think.

    def _make_entries_container(self):
        catalog = self

        class Entries:
            "Mock the dict interface around a MongoDB query result."
            def _doc_to_entry(self, run_start_doc):
                uid = run_start_doc['uid']
                run_stop_doc = catalog._run_stop_collection.find_one({'run_start': uid})
                if run_stop_doc is not None:
                    del run_stop_doc['_id']  # Drop internal Mongo detail.
                entry_metadata = {'start': run_start_doc,
                                  'stop': run_stop_doc}
                args = dict(
                    run_start_doc=run_start_doc,
                    get_run_stop=partial(catalog._get_run_stop, uid),
                    get_event_descriptors=partial(catalog._get_event_descriptors, uid),
                    get_event_cursor=catalog._get_event_cursor,
                    get_event_count=catalog._get_event_count,
                    get_resource=catalog._get_resource,
                    get_datum=catalog._get_datum,
                    get_datum_cursor=catalog._get_datum_cursor,
                    filler=catalog.filler)
                return intake.catalog.local.LocalCatalogEntry(
                    name=run_start_doc['uid'],
                    description={},  # TODO
                    driver='intake_bluesky.mongo_layout1.RunCatalog',  # TODO move to core
                    direct_access='forbid',  # ???
                    args=args,
                    cache=None,  # ???
                    parameters=[],
                    metadata=entry_metadata,
                    catalog_dir=None,
                    getenv=True,
                    getshell=True,
                    catalog=catalog)

            def __iter__(self):
                yield from self.keys()

            def keys(self):
                cursor = catalog._run_start_collection.find(
                    catalog._query, sort=[('time', pymongo.DESCENDING)])
                for run_start_doc in cursor:
                    yield run_start_doc['uid']

            def values(self):
                cursor = catalog._run_start_collection.find(
                    catalog._query, sort=[('time', pymongo.DESCENDING)])
                for run_start_doc in cursor:
                    del run_start_doc['_id']  # Drop internal Mongo detail.
                    yield self._doc_to_entry(run_start_doc)

            def items(self):
                cursor = catalog._run_start_collection.find(
                    catalog._query, sort=[('time', pymongo.DESCENDING)])
                for run_start_doc in cursor:
                    del run_start_doc['_id']  # Drop internal Mongo detail.
                    yield run_start_doc['uid'], self._doc_to_entry(run_start_doc)

            def __getitem__(self, name):
                # If this came from a client, we might be getting '-1'.
                try:
                    name = int(name)
                except ValueError:
                    pass
                if isinstance(name, int):
                    if name < 0:
                        # Interpret negative N as "the Nth from last entry".
                        query = catalog._query
                        cursor = (catalog._run_start_collection.find(query)
                                .sort('time', pymongo.DESCENDING) .limit(name))
                        *_, run_start_doc = cursor
                    else:
                        # Interpret positive N as
                        # "most recent entry with scan_id == N".
                        query = {'$and': [catalog._query, {'scan_id': name}]}
                        cursor = (catalog._run_start_collection.find(query)
                                .sort('time', pymongo.DESCENDING)
                                .limit(1))
                        run_start_doc, = cursor
                else:
                    query = {'$and': [catalog._query, {'uid': name}]}
                    run_start_doc = catalog._run_start_collection.find_one(query)
                if run_start_doc is None:
                    raise KeyError(name)
                del run_start_doc['_id']  # Drop internal Mongo detail.
                return self._doc_to_entry(run_start_doc)

            def __contains__(self, key):
                # Avoid iterating through all entries.
                try:
                    self[key]
                except KeyError:
                    return False
                else:
                    return True

        return Entries()

    def _close(self):
        self._client.close()

    def search(self, query):
        """
        Return a new Catalog with a subset of the entries in this Catalog.

        Parameters
        ----------
        query : dict
            MongoDB query.
        """
        if self._query:
            query = {'$and': [self._query, query]}
        cat = MongoMetadataStoreCatalog(
            metadatastore_uri=self._metadatastore_uri,
            asset_registry_uri=self._asset_registry_uri,
            query=query,
            getenv=self.getenv,
            getshell=self.getshell,
            auth=self.auth,
            metadata=(self.metadata or {}).copy(),
            storage_options=self.storage_options)
        cat.metadata['search'] = {'query': query, 'upstream': self.name}
        return cat


class RunCatalog(intake.catalog.Catalog):
    "represents one Run"
    container = 'bluesky-run-catalog'
    version = '0.0.1'
    partition_access = True
    PARTITION_SIZE = 100

    def __init__(self,
                 run_start_doc,
                 get_run_stop,
                 get_event_descriptors,
                 get_event_cursor,
                 get_event_count,
                 get_resource,
                 get_datum,
                 get_datum_cursor,
                 filler,
                 **kwargs):
        # All **kwargs are passed up to base class. TODO: spell them out
        # explicitly.
        self.urlpath = ''  # TODO Not sure why I had to add this.

        self._run_start_doc = run_start_doc
        self._get_run_stop = get_run_stop
        self._get_event_descriptors = get_event_descriptors
        self._get_event_cursor = get_event_cursor
        self._get_event_count = get_event_count
        self._get_resource = get_resource
        self._get_datum = get_datum
        self._get_datum_cursor = get_datum_cursor
        self.filler = filler
        super().__init__(**kwargs)
        

    def __repr__(self):
        try:
            start = self._run_start_doc
            stop = self._run_stop_doc or {}
            out = (f"<Intake catalog: Run {start['uid'][:8]}...>\n"
                   f"  {_ft(start['time'])} -- {_ft(stop.get('time', '?'))}\n")
                   # f"  Streams:\n")
            # for stream_name in self:
            #     out += f"    * {stream_name}\n"
        except Exception as exc:
            out = f"<Intake catalog: Run *REPR_RENDERING_FAILURE* {exc}>"
        return out

    def _load(self):
        # Count the total number of documents in this run.
        uid = self._run_start_doc['uid']
        self._run_stop_doc = self._get_run_stop()
        self._descriptors = self._get_event_descriptors()
        self._offset = len(self._descriptors) + 1
        self.metadata.update({'start': self._run_start_doc})
        self.metadata.update({'stop': self._run_stop_doc})

        count = 1
        descriptor_uids = [doc['uid'] for doc in self._descriptors]
        count += len(descriptor_uids)
        query = {'descriptor': {'$in': descriptor_uids}}
        count += self._get_event_count(
            [doc['uid'] for doc in self._descriptors])
        count += (self._run_stop_doc is not None)
        self.npartitions = int(numpy.ceil(count / self.PARTITION_SIZE))

        self._schema = intake.source.base.Schema(
            datashape=None,
            dtype=None,
            shape=(count,),
            npartitions=self.npartitions,
            metadata=self.metadata)

        # Sort descriptors like
        # {'stream_name': [descriptor1, descriptor2, ...], ...}
        streams = itertools.groupby(self._descriptors,
                                    key=lambda d: d.get('name'))

        # Make a MongoEventStreamSource for each stream_name.
        for stream_name, event_descriptor_docs in streams:
            args = dict(
                run_start_doc=self._run_start_doc,
                event_descriptor_docs=list(event_descriptor_docs),
                get_run_stop=self._get_run_stop,
                get_event_cursor=self._get_event_cursor,
                get_event_count=self._get_event_count,
                get_resource=self._get_resource,
                get_datum=self._get_datum,
                get_datum_cursor=self._get_datum_cursor,
                filler=self.filler,
                metadata={'descriptors': list(self._descriptors)},
                include='{{ include }}',
                exclude='{{ exclude }}')
            self._entries[stream_name] = intake.catalog.local.LocalCatalogEntry(
                name=stream_name,
                description={},  # TODO
                driver='intake_bluesky.mongo_layout1.MongoEventStream',  # TODO move to core
                direct_access='forbid',
                args=args,
                cache=None,  # ???
                parameters=[_INCLUDE_PARAMETER, _EXCLUDE_PARAMETER],
                metadata={'descriptors': list(self._descriptors)},
                catalog_dir=None,
                getenv=True,
                getshell=True,
                catalog=self)

    def read_canonical(self):
        ...

        return self._descriptors

    def read_partition(self, i):
        """Fetch one chunk of documents.
        """
        self._load()
        payload = []
        start = i * self.PARTITION_SIZE
        stop = (1 + i) * self.PARTITION_SIZE
        if start < self._offset:
            payload.extend(
                itertools.islice(
                    itertools.chain(
                        (('start', self._run_start_doc),),
                        (('descriptor', doc) for doc in self._descriptors)),
                    start,
                    stop))
        descriptor_uids = [doc['uid'] for doc in self._descriptors]
        skip = max(0, start - len(payload))
        limit = stop - start - len(payload)
        # print('start, stop, skip, limit', start, stop, skip, limit)
        if limit > 0:
            events = self._get_event_cursor(descriptor_uids, skip, limit)
            for event in events:
                try:
                    self.filler('event', event)
                except event_model.UnresolvableForeignKeyError as err:
                    datum_id = err.key
                    datum = self._get_datum(datum_id)
                    resource_uid = datum['resource']
                    resource = self._get_resource(resource_uid)
                    self.filler('resource', resource)
                    # Pre-fetch all datum for this resource.
                    for datum in self._get_datum_cursor(resource_uid):
                        self.filler('datum', datum)
                    # TODO -- When to clear the datum cache in filler?
                    self.filler('event', event)
                payload.append(('event', event))
            if i == self.npartitions - 1 and self._run_stop_doc is not None:
                payload.append(('stop', self._run_stop_doc))
        for _, doc in payload:
            doc.pop('_id', None)
        return payload


_EXCLUDE_PARAMETER = intake.catalog.local.UserParameter(
    name='exclude',
    description="fields to exclude",
    type='list',
    default=None)
_INCLUDE_PARAMETER = intake.catalog.local.UserParameter(
    name='include',
    description="fields to explicitly include at exclusion of all others",
    type='list',
    default=None)


class MongoEventStream(intake_xarray.base.DataSourceMixin):
    container = 'xarray'
    name = 'event-stream'
    version = '0.0.1'
    partition_access = True

    def __init__(self,
                 run_start_doc,
                 event_descriptor_docs,
                 get_run_stop,
                 get_event_cursor,
                 get_event_count,
                 get_resource,
                 get_datum,
                 get_datum_cursor,
                 filler,
                 metadata,
                 include,
                 exclude,
                 **kwargs):
        # self._partition_size = 10
        # self._default_chunks = 10
        self._run_start_doc = run_start_doc
        self._event_descriptor_docs = event_descriptor_docs
        self._get_run_stop = get_run_stop
        self._get_event_cursor = get_event_cursor
        self._get_event_count = get_event_count
        self._get_resource = get_resource
        self._get_datum = get_datum
        self._get_datum_cursor = get_datum_cursor
        self.filler = filler
        self.urlpath = ''  # TODO Not sure why I had to add this.
        self._ds = None  # set by _open_dataset below
        # TODO Is there a more direct way to get non-string UserParameters in?
        self.include = ast.literal_eval(include)
        self.exclude = ast.literal_eval(exclude)
        super().__init__(
            metadata=metadata
        )

    def __repr__(self):
        try:
            out = (f"<Intake catalog: Stream {self._stream_name!r} "
                   f"from Run {self._run_start_doc['uid'][:8]}...>")
        except Exception as exc:
            out = f"<Intake catalog: Stream *REPR_RENDERING_FAILURE* {exc}>"
        return out

    def _open_dataset(self):
        uid = self._run_start_doc['uid']
        self._run_stop_doc = self._get_run_stop()
        self.metadata.update({'start': self._run_start_doc})
        self.metadata.update({'stop': self._run_stop_doc})
        self._ds = documents_to_xarray(
            start_doc=self._run_start_doc,
            stop_doc=self._run_stop_doc,
            descriptor_docs=self._event_descriptor_docs,
            event_docs=list(self._get_event_cursor(
                [doc['uid'] for doc in self._event_descriptor_docs])),
            filler=self.filler,
            get_resource=self._get_resource,
            get_datum=self._get_datum,
            get_datum_cursor=self._get_datum_cursor,
            include=self.include,
            exclude=self.exclude)


def _transpose(in_data, keys, field):
    """Turn a list of dicts into dict of lists

    Parameters
    ----------
    in_data : list
        A list of dicts which contain at least one dict.
        All of the inner dicts must have at least the keys
        in `keys`

    keys : list
        The list of keys to extract

    field : str
        The field in the outer dict to use

    Returns
    -------
    transpose : dict
        The transpose of the data
    """
    out = {k: [None] * len(in_data) for k in keys}
    for j, ev in enumerate(in_data):
        dd = ev[field]
        for k in keys:
            out[k][j] = dd[k]
    return out

def _ft(timestamp):
    "format timestamp"
    if isinstance(timestamp, str):
        return timestamp
    # Truncate microseconds to miliseconds. Do not bother to round.
    return (datetime.fromtimestamp(timestamp)
            .strftime('%Y-%m-%d %H:%M:%S.%f'))[:-3]


def xarray_to_event_gen(data_xarr, ts_xarr, page_size):
    for start_idx in range(0, len(data_xarr['time']), page_size):
        stop_idx = start_idx + page_size
        data = {name: variable.values
                for name, variable in
                data_xarr.isel({'time': slice(start_idx, stop_idx)}).items()
                if ':' not in name}
        ts = {name: variable.values
              for name, variable in
              ts_xarr.isel({'time': slice(start_idx, stop_idx)}).items()
              if ':' not in name}
        event_page = {}
        seq_num = data.pop('seq_num')
        ts.pop('seq_num')
        uids = data.pop('uid')
        ts.pop('uid')
        event_page['data'] = data
        event_page['timestamps'] = ts
        event_page['time'] = data_xarr['time'][start_idx:stop_idx].values
        event_page['uid'] = uids
        event_page['seq_num'] = seq_num
        event_page['filled'] = {}

        yield event_page


intake.registry['remote-bluesky-run-catalog'] = RemoteRunCatalog
intake.container.container_map['bluesky-run-catalog'] = RemoteRunCatalog
intake.registry['mongo_metadatastore'] = MongoMetadataStoreCatalog
