import logging
import os
import socket
import sqlite3
from datetime import date, datetime, time, timedelta
from os.path import normpath, split
from uuid import uuid4

import icalendar
from atomicwrites import AtomicWriter
from dateutil.tz import tzlocal

logger = logging.getLogger(name=__name__)


# Initialize this only once
# We were doing this all over the place (even if unused!), so at least only do
# it once.
LOCAL_TIMEZONE = tzlocal()


class NoSuchTodo(Exception):
    pass


class ReadOnlyTodo(Exception):
    pass


class UnsafeOperationException(Exception):
    """
    Raised when attempting to perform an unsafe operation.

    Typical examples of unsafe operations are attempting to save a
    partially-loaded todo.
    """
    pass


class AlreadyExists(Exception):
    """
    Raise when two objects have a same identity.

    This can ocurrs when two lists have the same name, or when two Todos have
    the same path.
    """
    pass


class cached_property:  # noqa
    '''A read-only @property that is only evaluated once. Only usable on class
    instances' methods.
    '''
    def __init__(self, fget, doc=None):
        self.__name__ = fget.__name__
        self.__module__ = fget.__module__
        self.__doc__ = doc or fget.__doc__
        self.fget = fget

    def __get__(self, obj, cls):
        if obj is None:
            return self
        obj.__dict__[self.__name__] = result = self.fget(obj)
        return result


class Todo:
    """
    Represents a task/todo, and wrapps around icalendar.Todo.

    All text attributes are always treated as text, and "" will be returned if
    they are not defined.
    Date attributes are treated as datetime objects, and None will be returned
    if they are not defined.
    All datetime objects have tzinfo, either the one defined in the file, or
    the local system's one.
    """

    def __init__(self, filename=None, mtime=None, new=False):
        """
        Creates a new todo using `todo` as a source.

        :param str filename: The name of the file for this todo. Defaults to
        the <uid>.ics
        :param mtime int: The last modified time for the file backing this
        Todo.
        :param bool new: Indicate that a new Todo is being created and should
        be populated with default values.
        """

        self._attributes = {}

        now = datetime.now(LOCAL_TIMEZONE)
        self.uid = '{}@{}'.format(uuid4().hex, socket.gethostname())

        if new:
            self.created_at = now

        if not self.dtstamp:
            self.dtstamp = now

        self.filename = filename or "{}.ics".format(self.uid)

        if os.path.basename(self.filename) != self.filename:
            raise ValueError(
                'Must not be an absolute path: {}' .format(self.filename)
            )
        self.mtime = mtime or datetime.now()

    DEFAULT_VALUES = {
        'categories': [],
        'completed_at': None,
        'created_at': None,
        'description': '',
        'dtstamp': None,
        'start': None,
        'due': None,
        'location': '',
        'percent_complete': 0,
        'priority': 0,
        'sequence': 0,
        'status': 'NEEDS-ACTION',
        'summary': '',
        'uid': None,
        'id': None,  # Not the right place
    }

    KNOWN_FIELDS = DEFAULT_VALUES.keys()

    def __getattr__(self, name):
        if name in Todo.KNOWN_FIELDS:
            if name in self._attributes:
                return self._attributes[name]
            else:
                return Todo.DEFAULT_VALUES[name]
        raise AttributeError("'{}' has no attribute '{}'".format(self, name))

    def __setattr__(self, name, value):
        if name in Todo.KNOWN_FIELDS:
            # In some cases we want value?
            if value != Todo.DEFAULT_VALUES[name] and value:
                self._attributes[name] = value
            elif name in self._attributes:
                del(self._attributes[name])
        else:
            object.__setattr__(self, name, value)

    @property
    def is_completed(self):
        return (
            bool(self.completed_at) or
            self.status in ('CANCELLED', 'COMPLETED')
        )

    @is_completed.setter
    def is_completed(self, val):
        if val:
            # Don't fiddle with completed_at if this was already completed:
            if not self.is_completed:
                self.completed_at = datetime.now(tz=LOCAL_TIMEZONE)
            self.percent_complete = 100
            self.status = 'COMPLETED'
        else:
            self.completed_at = None
            self.percent_complete = None
            self.status = 'NEEDS-ACTION'

    def save(self, list_=None):
        list_ = list_ or self.list
        path = os.path.join(list_.path, self.filename)
        assert path.startswith(list_.path)

        self.sequence += 1

        return VtodoWritter(self).write(path)


class VtodoWritter:
    """Writes a Todo as a VTODO file."""

    DATETIME_FIELDS = [
        'completed',
        'created',
        'created_at',
        'dtstamp',
        'dtstart',
        'due',
    ]
    STRING_FIELDS = [
        'description',
        'location',
        'status',
        'summary',
        'uid',
    ]
    INT_FIELDS = [
        'percent_complete',
        'priority',
        'sequence',
    ]
    LIST_FIELDS = [
        'categories',
    ]

    """Maps Todo field names to VTODO field names"""
    FIELD_MAP = {
        'summary': 'summary',
        'priority': 'priority',
        'sequence': 'sequence',
        'uid': 'uid',
        'categories': 'categories',
        'completed_at': 'completed',
        'description': 'description',
        'dtstamp': 'dtstamp',
        'start': 'dtstart',
        'due': 'due',
        'location': 'location',
        'percent_complete': 'percent-complete',
        'priority': 'priority',
        'status': 'status',
        'created_at': 'created',
    }

    def __init__(self, todo):
        self.todo = todo

    def normalize_datetime(self, dt):
        '''
        Eliminate several differences between dates, times and datetimes which
        are hindering comparison:

        - Convert everything to datetime
        - Add missing timezones
        '''
        if isinstance(dt, date) and not isinstance(dt, datetime):
            dt = datetime(dt.year, dt.month, dt.day)
        # XXX: Can we actually get times from the UI?
        elif isinstance(dt, time):
            dt = datetime.combine(date.today(), dt)

        if not dt.tzinfo:
            dt = dt.replace(tzinfo=LOCAL_TIMEZONE)

        return dt

    def serialize_field(self, name, value):
        if name in VtodoWritter.DATETIME_FIELDS:
            return self.normalize_datetime(value)
        if name in VtodoWritter.LIST_FIELDS:
            return ','.join(value)
        if name in VtodoWritter.INT_FIELDS:
            return int(value)
        if name in VtodoWritter.STRING_FIELDS:
            return value

        logger.warn('Unknown field %s serialized.', name)
        return value

    def set_field(self, name, value):
        value = self.serialize_field(name, value)

        if name in self.vtodo:
            del(self.vtodo[name])
        if value:
            logger.debug("Setting field %s to %s.", name, value)
            self.vtodo.add(name, value)

    def serialize(self, original=None):
        """Serialize a Todo into a VTODO."""
        if not original:
            original = icalendar.Todo()
        self.vtodo = original

        for source, target in self.FIELD_MAP.items():
            if getattr(self.todo, source):
                self.set_field(target, getattr(self.todo, source))

        return self.vtodo

    def _read(self, path):
        with open(path, 'rb') as f:
            cal = f.read()
            cal = icalendar.Calendar.from_ical(cal)
            for component in cal.walk('VTODO'):
                return component

    def write(self, path):
        if os.path.exists(path):
            self._write_existing(path)
        else:
            self._write_new(path)

        return self.vtodo

    def _write_existing(self, path):
        original = self._read(path)
        vtodo = self.serialize(original)

        with open(path, 'rb') as f:
            cal = icalendar.Calendar.from_ical(f.read())
            for index, component in enumerate(cal.subcomponents):
                if component.get('uid', None) == self.todo.uid:
                    cal.subcomponents[index] = vtodo

        with AtomicWriter(path, overwrite=True).open() as f:
            f.write(cal.to_ical().decode("UTF-8"))

    def _write_new(self, path):
        vtodo = self.serialize()

        c = icalendar.Calendar()
        c.add_component(vtodo)

        with AtomicWriter(path).open() as f:
            c.add('prodid', 'io.barrera.todoman')
            c.add('version', '2.0')
            f.write(c.to_ical().decode("UTF-8"))

        return vtodo


class Cache:
    """
    Caches Todos for faster read and simpler querying interface

    The Cache class persists relevant[1] fields into an SQL database, which is
    only updated if the actual file has been modified. This greatly increases
    load times, but, more importantly, provides a simpler interface for
    filtering/querying/sorting.

    The internal sqlite database is copied fully into memory at startup, and
    dumped again at showdown. This reduces excessive disk I/O.

    [1]: Relevant fields are those we show when listing todos, or those which
    may be used for filtering/sorting.
    """

    SCHEMA_VERSION = 2

    def __init__(self, path):
        self.cache_path = str(path)
        os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)

        self._conn = sqlite3.connect(self.cache_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")

        self.create_tables()

    def save_to_disk(self):
        self._conn.commit()

    def is_latest_version(self):
        """Checks if the cache DB schema is the latest version."""
        try:
            return self._conn.execute(
                'SELECT version FROM meta WHERE version = ?',
                (Cache.SCHEMA_VERSION,),
            ).fetchone()
        except sqlite3.OperationalError:
            return False

    def create_tables(self):
        if self.is_latest_version():
            return

        self._conn.executescript('''
            DROP TABLE IF EXISTS lists;
            DROP TABLE IF EXISTS files;
            DROP TABLE IF EXISTS todos;
        ''')

        self._conn.execute(
            'CREATE TABLE IF NOT EXISTS meta ("version" INT)'
        )

        self._conn.execute(
            'INSERT INTO meta (version) VALUES (?)',
            (Cache.SCHEMA_VERSION,),
        )

        self._conn.execute('''
            CREATE TABLE IF NOT EXISTS lists (
                "name" TEXT PRIMARY KEY,
                "path" TEXT,
                "colour" TEXT,
                CONSTRAINT path_unique UNIQUE (path)
            );
        ''')

        self._conn.execute('''
            CREATE TABLE IF NOT EXISTS files (
                "path" TEXT PRIMARY KEY,
                "list_name" TEXT,
                "mtime" INTEGER,

                CONSTRAINT path_unique UNIQUE (path),
                FOREIGN KEY(list_name) REFERENCES lists(name) ON DELETE CASCADE
            );
        ''')

        self._conn.execute('''
            CREATE TABLE IF NOT EXISTS todos (
                "file_path" TEXT,

                "id" INTEGER PRIMARY KEY,
                "uid" TEXT,
                "summary" TEXT,
                "due" INTEGER,
                "priority" INTEGER,
                "created_at" INTEGER,
                "completed_at" INTEGER,
                "percent_complete" INTEGER,
                "dtstamp" INTEGER,
                "status" TEXT,
                "description" TEXT,
                "location" TEXT,
                "categories" TEXT,

                FOREIGN KEY(file_path) REFERENCES files(path) ON DELETE CASCADE
            );
        ''')

    def clear(self):
        self._conn.close()
        os.remove(self.cache_path)
        self._conn = None

    def add_list(self, name, path, colour):
        """
        Inserts a new list into the cache.

        Returns the id of the newly inserted list.
        """

        result = self._conn.execute(
            'SELECT name FROM lists WHERE path = ?',
            (path,),
        ).fetchone()

        if result:
            return result['name']

        try:
            self._conn.execute(
                "INSERT INTO lists (name, path, colour) VALUES (?, ?, ?)",
                (name, path, colour,),
            )
        except sqlite3.IntegrityError as e:
            raise AlreadyExists(name) from e

        return self.add_list(name, path, colour)

    def add_file(self, list_name, path, mtime):
        try:
            self._conn.execute('''
                INSERT INTO files (
                    list_name,
                    path,
                    mtime
                ) VALUES (?, ?, ?);
                ''', (
                list_name,
                path,
                mtime,
            ))
        except sqlite3.IntegrityError as e:
            raise AlreadyExists(list_name) from e

    def _serialize_datetime(self, todo, field):
        dt = todo.decoded(field, None)
        if not dt:
            return None

        if isinstance(dt, date) and not isinstance(dt, datetime):
            dt = datetime(dt.year, dt.month, dt.day)
        # XXX: Can we actually read times from files?
        elif isinstance(dt, time):
            dt = datetime.combine(date.today(), dt)

        if not dt.tzinfo:
            dt = dt.replace(tzinfo=LOCAL_TIMEZONE)
        return dt.timestamp()

    def add_vtodo(self, todo, file_path):
        """
        Adds a todo into the cache.

        :param icalendar.Todo todo: The icalendar component object on which
        """

        sql = '''
            INSERT INTO todos (
                file_path,
                uid,
                summary,
                due,
                priority,
                created_at,
                completed_at,
                percent_complete,
                dtstamp,
                status,
                description,
                location,
                categories
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            '''

        params = (
            file_path,
            todo.get('uid'),
            todo.get('summary'),
            self._serialize_datetime(todo, 'due'),
            todo.get('priority', None),
            self._serialize_datetime(todo, 'created'),
            self._serialize_datetime(todo, 'completed'),
            todo.get('percent-complete', None),
            self._serialize_datetime(todo, 'dtstamp'),
            todo.get('status', None),
            todo.get('description', None),
            todo.get('location', None),
            todo.get('categories', None),
        )

        cursor = self._conn.cursor()
        try:
            cursor.execute(sql, params)
            rv = cursor.lastrowid
        finally:
            cursor.close()

        return rv

    def todos(self, all=False, lists=[], priority=None, location='',
              category='', grep='', sort=[], reverse=True, due=None,
              done_only=None, start=None):
        """
        Returns filtered cached todos, in a specified order.

        If no order is specified, todos are sorted by the following fields::

            completed_at
            -priority
            due
            -created_at

        :param bool all: If true, also return completed todos.
        :param list lists: Only return todos for these lists.
        :param str location: Only return todos with a location containing this
            string.
        :param str category: Only return todos with a category containing this
            string.
        :param str grep: Filter common fields with this substring.
        :param list sort: Order returned todos by these fields. Field names
            with a ``-`` prepended will be used to sort in reverse order.
        :param bool reverse: Reverse the order of the todos after sorting.
        :param int due: Return only todos due within ``due`` hours.
        :param str priority: Only return todos with priority at least as
            high as specified.
        :param bool done_only: If true, return done tasks, else incomplete
            tasks
        :param start: Return only todos before/after ``start`` date
        :return: A sorted, filtered list of todos.
        :rtype: generator
        """
        extra_where = []
        params = []

        if not all:
            # XXX: Duplicated logic of Todo.is_completed
            if done_only:
                extra_where.append('AND status == "COMPLETED"')
            else:
                extra_where.append('AND completed_at is NULL '
                                   'AND status IS NOT "CANCELLED" '
                                   'AND status IS NOT "COMPLETED"')
        if lists:
            lists = [l.name if isinstance(l, List) else l for l in lists]
            q = ', '.join(['?'] * len(lists))
            extra_where.append('AND files.list_name IN ({})'.format(q))
            params.extend(lists)
        if priority:
            extra_where.append('AND PRIORITY > 0 AND PRIORITY <= ?')
            params.append('{}'.format(priority))
        if location:
            extra_where.append('AND location LIKE ?')
            params.append('%{}%'.format(location))
        if category:
            extra_where.append('AND categories LIKE ?')
            params.append('%{}%'.format(category))
        if grep:
            # # requires sqlite with pcre, which won't be available everywhere:
            # extra_where.append('AND summary REGEXP ?')
            # params.append(grep)
            extra_where.append('AND summary LIKE ?')
            params.append('%{}%'.format(grep))
        if due:
            max_due = (datetime.now() + timedelta(hours=due)).timestamp()
            extra_where.append('AND due IS NOT NULL AND due < ?')
            params.append(max_due)
        if start:
            is_before, dt = start
            dt = dt.timestamp()
            if is_before:
                extra_where.append('AND created_at <= ?')
                params.append(dt)
            else:
                extra_where.append('AND created_at >= ?')
                params.append(dt)
        if sort:
            order = []
            for s in sort:
                if s.startswith('-'):
                    order.append(' {} ASC'.format(s[1:]))
                else:
                    order.append(' {} DESC'.format(s))
            order = ','.join(order)
        else:
            order = '''
                completed_at DESC,
                priority IS NOT NULL, priority DESC,
                due IS NOT NULL, due DESC,
                created_at ASC
            '''

        if not reverse:
            # Note the change in case to avoid swapping all of them. sqlite
            # doesn't care about casing anyway.
            order = order.replace(' DESC', ' asc').replace(' ASC', ' desc')

        query = '''
              SELECT todos.*, files.list_name, files.path
                FROM todos, files
               WHERE todos.file_path = files.path {}
            ORDER BY {}
        '''.format(' '.join(extra_where), order,)
        result = self._conn.execute(query, params)

        seen_paths = set()
        warned_paths = set()

        for row in result:
            todo = self._todo_from_db(row)
            path = row['path']

            if path in seen_paths and path not in warned_paths:
                logger.warning('Todo is in read-only mode because there are '
                               'multiple todos in %s', path)
                warned_paths.add(path)
            seen_paths.add(path)
            yield todo

    def _dt_from_db(self, dt):
        if dt:
            return datetime.fromtimestamp(dt, LOCAL_TIMEZONE)
        return None

    def _todo_from_db(self, row):
        todo = Todo()
        todo.id = row['id']
        todo.uid = row['uid']
        todo.summary = row['summary']
        todo.due = self._dt_from_db(row['due'])
        todo.priority = row['priority']
        todo.created_at = self._dt_from_db(row['created_at'])
        todo.completed_at = self._dt_from_db(row['completed_at'])
        todo.dtstamp = self._dt_from_db(row['dtstamp'])
        todo.percent_complete = row['percent_complete']
        todo.status = row['status']
        todo.description = row['description']
        todo.location = row['location']
        todo.list = self.lists_map[row['list_name']]
        todo.filename = os.path.basename(row['path'])
        return todo

    def lists(self):
        result = self._conn.execute("SELECT * FROM lists")
        for row in result:
            yield List(
                name=row['name'],
                path=row['path'],
                colour=row['colour'],
            )

    @cached_property
    def lists_map(self):
        return {l.name: l for l in self.lists()}

    def list(self, name):
        row = self._conn.execute(
            "SELECT * FROM lists WHERE name = ?",
            (name,),
        ).fetchone()

        return List(
            name=row['name'],
            path=row['path'],
            colour=row['colour'],
        )

    def expire_lists(self, paths):
        results = self._conn.execute("SELECT path, name from lists")
        for result in results:
            if result['path'] not in paths:
                self.delete_list(result['name'])

    def delete_list(self, name):
        self._conn.execute("DELETE FROM lists WHERE lists.name = ?", (name,))

    def todo(self, id, read_only=False):
        # XXX: DON'T USE READ_ONLY
        result = self._conn.execute('''
            SELECT todos.*, files.list_name, files.path
              FROM todos, files
            WHERE files.path = todos.file_path
              AND todos.id = ?
        ''', (id,)
        ).fetchone()

        if not result:
            raise NoSuchTodo(id)

        if not read_only:
            count = self._conn.execute('''
                SELECT count(id) AS c
                  FROM files, todos
                 WHERE todos.file_path = files.path
                   AND path=?
            ''', (result['path'],)
            ).fetchone()
            if count['c'] > 1:
                raise ReadOnlyTodo()

        return self._todo_from_db(result)

    def expire_files(self, paths_to_mtime):
        """Remove stale cache entries based on the given fresh data."""
        result = self._conn.execute("SELECT path, mtime FROM files")
        for row in result:
            path, mtime = row['path'], row['mtime']
            if paths_to_mtime.get(path, None) != mtime:
                self.expire_file(path)

    def expire_file(self, path):
        self._conn.execute("DELETE FROM files WHERE path = ?", (path,))


class List:

    def __init__(self, name, path, colour=None):
        self.name = name
        self.path = path
        self.colour = colour

    @cached_property
    def color_raw(self):
        '''
        The color is a file whose content is of the format `#RRGGBB`.
        '''

        try:
            with open(os.path.join(self.path, 'color')) as f:
                return f.read().strip()
        except (OSError, IOError):
            logger.debug('No colour for list %', self.path)

    @cached_property
    def color_rgb(self):
        color = self.colour or self.color_raw
        if not color or not color.startswith('#'):
            return

        r = color[1:3]
        g = color[3:5]
        b = color[5:8]

        if len(r) == len(g) == len(b) == 2:
            return int(r, 16), int(g, 16), int(b, 16)

    @cached_property
    def color_ansi(self):
        rv = self.color_rgb
        if rv:
            return '\33[38;2;{!s};{!s};{!s}m'.format(*rv)

    def __str__(self):
        return self.name


class Database:
    """
    This class is essentially a wrapper around all the lists (which in turn,
    contain all the todos).

    Caching in abstracted inside this class, and is transparent to outside
    classes.
    """

    def __init__(self, paths, cache_path):
        self.cache = Cache(cache_path)
        self.paths = [str(path) for path in paths]
        self.update_cache()

    def update_cache(self):
        self.cache.expire_lists(self.paths)

        paths_to_mtime = {}
        paths_to_list_name = {}

        for path in self.paths:
            list_name = self.cache.add_list(
                self._list_name(path),
                path,
                self._list_colour(path),
            )
            for entry in os.listdir(path):
                if not entry.endswith('.ics'):
                    continue
                entry_path = os.path.join(path, entry)
                mtime = _getmtime(entry_path)
                paths_to_mtime[entry_path] = mtime
                paths_to_list_name[entry_path] = list_name

        self.cache.expire_files(paths_to_mtime)

        for entry_path, mtime in paths_to_mtime.items():
            list_name = paths_to_list_name[entry_path]

            try:
                self.cache.add_file(list_name, entry_path, mtime)
            except AlreadyExists:
                logger.debug('File already in cache: %s', entry_path)
                continue

            with open(entry_path, 'rb') as f:
                try:
                    cal = f.read()
                    cal = icalendar.Calendar.from_ical(cal)
                    for component in cal.walk('VTODO'):
                        self.cache.add_vtodo(component, entry_path)
                except Exception as e:
                    logger.exception("Failed to read entry %s.", entry_path)

        self.cache.save_to_disk()

    def todos(self, **kwargs):
        return self.cache.todos(**kwargs)

    def todo(self, id, **kwargs):
        return self.cache.todo(id, **kwargs)

    def lists(self):
        return self.cache.lists()

    def move(self, todo, new_list, from_list=None):
        from_list = from_list or todo.list
        orig_path = os.path.join(from_list.path, todo.filename)
        dest_path = os.path.join(new_list.path, todo.filename)

        os.rename(orig_path, dest_path)

    def delete(self, todo):
        path = os.path.join(todo.list.path, todo.filename)
        os.remove(path)

    def _list_name(self, path):
        # XXX: TODO: dedupe this!
        try:
            with open(os.path.join(path, 'displayname')) as f:
                return f.read().strip()
        except (OSError, IOError):
            return split(normpath(path))[1]

    def _list_colour(self, path):
        '''
        The color is a file whose content is of the format `#RRGGBB`.
        '''
        # XXX: TODO: dedupe this!

        try:
            with open(os.path.join(path, 'color')) as f:
                return f.read().strip()
        except (OSError, IOError):
            logger.debug('No colour for list %s', path)

    def flush(self):
        for todo in self.todos(all=True):
            if todo.is_completed:
                yield todo
                self.delete(todo)

        self.cache.clear()
        self.cache = None

    def save(self, todo, list_):
        vtodo = todo.save(list_)
        path = os.path.join(list_.path, todo.filename)
        self.cache.expire_file(path)
        mtime = _getmtime(path)
        self.cache.add_file(list_.name, path, mtime)
        id = self.cache.add_vtodo(vtodo, path)
        todo.id = id
        self.cache.save_to_disk()


def _getmtime(path):
    stat = os.stat(path)
    return getattr(stat, 'st_mtime_ns', stat.st_mtime)
