from __future__ import print_function, unicode_literals
import logging
import re
import itertools
import operator
from collections import namedtuple
from pgspecial.namedqueries import NamedQueries
from prompt_toolkit.completion import Completer, Completion
from .packages.sqlcompletion import (
    suggest_type, Special, Database, Schema, Table, Function, Column, View,
    Keyword, NamedQuery, Datatype, Alias)
from .packages.parseutils import last_word
from .packages.pgliterals.main import get_literals
from .packages.prioritization import PrevalenceCounter
from .config import load_config, config_location

try:
    from collections import Counter
except ImportError:
    # python 2.6
    from .packages.counter import Counter

_logger = logging.getLogger(__name__)

NamedQueries.instance = NamedQueries.from_config(
    load_config(config_location() + 'config'))


Match = namedtuple('Match', ['completion', 'priority'])


class PGCompleter(Completer):
    keywords = get_literals('keywords')
    functions = get_literals('functions')
    datatypes = get_literals('datatypes')

    def __init__(self, smart_completion=True, pgspecial=None):
        super(PGCompleter, self).__init__()
        self.smart_completion = smart_completion
        self.pgspecial = pgspecial
        self.prioritizer = PrevalenceCounter()

        self.reserved_words = set()
        for x in self.keywords:
            self.reserved_words.update(x.split())
        self.name_pattern = re.compile("^[_a-z][_a-z0-9\$]*$")

        self.databases = []
        self.dbmetadata = {'tables': {}, 'views': {}, 'functions': {},
                           'datatypes': {}}
        self.search_path = []

        self.all_completions = set(self.keywords + self.functions)

    def escape_name(self, name):
        if name and ((not self.name_pattern.match(name))
                or (name.upper() in self.reserved_words)
                or (name.upper() in self.functions)):
            name = '"%s"' % name

        return name

    def unescape_name(self, name):
        """ Unquote a string."""
        if name and name[0] == '"' and name[-1] == '"':
            name = name[1:-1]

        return name

    def escaped_names(self, names):
        return [self.escape_name(name) for name in names]

    def extend_database_names(self, databases):
        databases = self.escaped_names(databases)
        self.databases.extend(databases)

    def extend_keywords(self, additional_keywords):
        self.keywords.extend(additional_keywords)
        self.all_completions.update(additional_keywords)

    def extend_schemata(self, schemata):

        # schemata is a list of schema names
        schemata = self.escaped_names(schemata)
        metadata = self.dbmetadata['tables']
        for schema in schemata:
            metadata[schema] = {}

        # dbmetadata.values() are the 'tables' and 'functions' dicts
        for metadata in self.dbmetadata.values():
            for schema in schemata:
                metadata[schema] = {}

        self.all_completions.update(schemata)

    def extend_relations(self, data, kind):
        """ extend metadata for tables or views

        :param data: list of (schema_name, rel_name) tuples
        :param kind: either 'tables' or 'views'
        :return:
        """

        data = [self.escaped_names(d) for d in data]

        # dbmetadata['tables']['schema_name']['table_name'] should be a list of
        # column names. Default to an asterisk
        metadata = self.dbmetadata[kind]
        for schema, relname in data:
            try:
                metadata[schema][relname] = ['*']
            except KeyError:
                _logger.error('%r %r listed in unrecognized schema %r',
                              kind, relname, schema)
            self.all_completions.add(relname)

    def extend_columns(self, column_data, kind):
        """ extend column metadata

        :param column_data: list of (schema_name, rel_name, column_name) tuples
        :param kind: either 'tables' or 'views'
        :return:
        """

        column_data = [self.escaped_names(d) for d in column_data]
        metadata = self.dbmetadata[kind]
        for schema, relname, column in column_data:
            metadata[schema][relname].append(column)
            self.all_completions.add(column)

    def extend_functions(self, func_data):

        # func_data is a list of function metadata namedtuples
        # with fields schema_name, func_name, arg_list, result,
        # is_aggregate, is_window, is_set_returning

        # dbmetadata['schema_name']['functions']['function_name'] should return
        # the function metadata namedtuple for the corresponding function
        metadata = self.dbmetadata['functions']

        for f in func_data:
            schema, func = self.escaped_names([f.schema_name, f.func_name])

            if func in metadata[schema]:
                metadata[schema][func].append(f)
            else:
                metadata[schema][func] = [f]

            self.all_completions.add(func)

    def extend_datatypes(self, type_data):

        # dbmetadata['datatypes'][schema_name][type_name] should store type
        # metadata, such as composite type field names. Currently, we're not
        # storing any metadata beyond typename, so just store None
        meta = self.dbmetadata['datatypes']

        for t in type_data:
            schema, type_name = self.escaped_names(t)
            meta[schema][type_name] = None
            self.all_completions.add(type_name)

    def extend_query_history(self, text, is_init=False):
        if is_init:
            # During completer initialization, only load keyword preferences,
            # not names
            self.prioritizer.update_keywords(text)
        else:
            self.prioritizer.update(text)

    def set_search_path(self, search_path):
        self.search_path = self.escaped_names(search_path)

    def reset_completions(self):
        self.databases = []
        self.special_commands = []
        self.search_path = []
        self.dbmetadata = {'tables': {}, 'views': {}, 'functions': {},
                           'datatypes': {}}
        self.all_completions = set(self.keywords + self.functions)

    def find_matches(self, text, collection, mode='fuzzy',
                     meta=None, meta_collection=None):
        """Find completion matches for the given text.

        Given the user's input text and a collection of available
        completions, find completions matching the last word of the
        text.

        `mode` can be either 'fuzzy', or 'strict'
            'fuzzy': fuzzy matching, ties broken by name prevalance
            `keyword`: start only matching, ties broken by keyword prevalance

        yields prompt_toolkit Completion instances for any matches found
        in the collection of available completions.

        """

        text = last_word(text, include='most_punctuations').lower()
        text_len = len(text)

        if text and text[0] == '"':
            # text starts with double quote; user is manually escaping a name
            # Match on everything that follows the double-quote. Note that
            # text_len is calculated before removing the quote, so the
            # Completion.position value is correct
            text = text[1:]

        if mode == 'fuzzy':
            fuzzy = True
            priority_func = self.prioritizer.name_count
        else:
            fuzzy = False
            priority_func = self.prioritizer.keyword_count

        # Construct a `_match` function for either fuzzy or non-fuzzy matching
        # The match function returns a 2-tuple used for sorting the matches,
        # or None if the item doesn't match
        # Note: higher priority values mean more important, so use negative
        # signs to flip the direction of the tuple
        if fuzzy:
            regex = '.*?'.join(map(re.escape, text))
            pat = re.compile('(%s)' % regex)

            def _match(item):
                r = pat.search(self.unescape_name(item.lower()))
                if r:
                    return -len(r.group()), -r.start()
        else:
            match_end_limit = len(text)

            def _match(item):
                match_point = item.lower().find(text, 0, match_end_limit)
                if match_point >= 0:
                    # Use negative infinity to force keywords to sort after all
                    # fuzzy matches
                    return -float('Infinity'), -match_point

        if meta_collection:
            # Each possible completion in the collection has a corresponding
            # meta-display string
            collection = zip(collection, meta_collection)
        else:
            # All completions have an identical meta
            collection = zip(collection, itertools.repeat(meta))

        matches = []

        for item, meta in collection:
            sort_key = _match(item)
            if sort_key:
                if meta and len(meta) > 50:
                    # Truncate meta-text to 50 characters, if necessary
                    meta = meta[:47] + u'...'

                # Lexical order of items in the collection, used for
                # tiebreaking items with the same match group length and start
                # position. Since we use *higher* priority to mean "more
                # important," we use -ord(c) to prioritize "aa" > "ab" and end
                # with 1 to prioritize shorter strings (ie "user" > "users").
                # We also use the unescape_name to make sure quoted names have
                # the same priority as unquoted names.
                lexical_priority = tuple(-ord(c) for c in self.unescape_name(item)) + (1,)

                priority = sort_key, priority_func(item), lexical_priority

                matches.append(Match(
                    completion=Completion(item, -text_len, display_meta=meta),
                    priority=priority))

        return matches

    def get_completions(self, document, complete_event, smart_completion=None):
        word_before_cursor = document.get_word_before_cursor(WORD=True)

        if smart_completion is None:
            smart_completion = self.smart_completion

        # If smart_completion is off then match any word that starts with
        # 'word_before_cursor'.
        if not smart_completion:
            matches = self.find_matches(word_before_cursor, self.all_completions,
                                        mode='strict')
            completions = [m.completion for m in matches]
            return sorted(completions, key=operator.attrgetter('text'))

        matches = []
        suggestions = suggest_type(document.text, document.text_before_cursor)

        for suggestion in suggestions:
            suggestion_type = type(suggestion)
            _logger.debug('Suggestion type: %r', suggestion_type)

            # Map suggestion type to method
            # e.g. 'table' -> self.get_table_matches
            matcher = self.suggestion_matchers[suggestion_type]
            matches.extend(matcher(self, suggestion, word_before_cursor))

        # Sort matches so highest priorities are first
        matches = sorted(matches, key=operator.attrgetter('priority'),
                         reverse=True)

        return [m.completion for m in matches]

    def get_column_matches(self, suggestion, word_before_cursor):
        tables = suggestion.tables
        _logger.debug("Completion column scope: %r", tables)
        scoped_cols = self.populate_scoped_cols(tables)

        if suggestion.drop_unique:
            # drop_unique is used for 'tb11 JOIN tbl2 USING (...' which should
            # suggest only columns that appear in more than one table
            scoped_cols = [col for (col, count)
                                 in Counter(scoped_cols).items()
                                   if count > 1 and col != '*']

        return self.find_matches(word_before_cursor, scoped_cols, meta='column')

    def get_function_matches(self, suggestion, word_before_cursor):
        if suggestion.filter == 'is_set_returning':
            # Only suggest set-returning functions
            filt = operator.attrgetter('is_set_returning')
            funcs = self.populate_functions(suggestion.schema, filt)
        else:
            funcs = self.populate_schema_objects(
                suggestion.schema, 'functions')

        # Function overloading means we way have multiple functions of the same
        # name at this point, so keep unique names only
        funcs = set(funcs)

        funcs = self.find_matches(word_before_cursor, funcs, meta='function')

        if not suggestion.schema and not suggestion.filter:
            # also suggest hardcoded functions using startswith matching
            predefined_funcs = self.find_matches(
                word_before_cursor, self.functions, mode='strict',
                meta='function')
            funcs.extend(predefined_funcs)

        return funcs

    def get_schema_matches(self, _, word_before_cursor):
        schema_names = self.dbmetadata['tables'].keys()

        # Unless we're sure the user really wants them, hide schema names
        # starting with pg_, which are mostly temporary schemas
        if not word_before_cursor.startswith('pg_'):
            schema_names = [s for s in schema_names if not s.startswith('pg_')]

        return self.find_matches(word_before_cursor, schema_names, meta='schema')

    def get_table_matches(self, suggestion, word_before_cursor):
        tables = self.populate_schema_objects(suggestion.schema, 'tables')

        # Unless we're sure the user really wants them, don't suggest the
        # pg_catalog tables that are implicitly on the search path
        if not suggestion.schema and (
                not word_before_cursor.startswith('pg_')):
            tables = [t for t in tables if not t.startswith('pg_')]

        return self.find_matches(word_before_cursor, tables, meta='table')

    def get_view_matches(self, suggestion, word_before_cursor):
        views = self.populate_schema_objects(suggestion.schema, 'views')

        if not suggestion.schema and (
                not word_before_cursor.startswith('pg_')):
            views = [v for v in views if not v.startswith('pg_')]

        return self.find_matches(word_before_cursor, views, meta='view')

    def get_alias_matches(self, suggestion, word_before_cursor):
        aliases = suggestion.aliases
        return self.find_matches(word_before_cursor, aliases,
                                 meta='table alias')

    def get_database_matches(self, _, word_before_cursor):
        return self.find_matches(word_before_cursor, self.databases,
                                 meta='database')

    def get_keyword_matches(self, _, word_before_cursor):
        return self.find_matches(word_before_cursor, self.keywords,
                                 mode='strict', meta='keyword')

    def get_special_matches(self, _, word_before_cursor):
        if not self.pgspecial:
            return []

        commands = self.pgspecial.commands
        cmd_names = commands.keys()
        desc = [commands[cmd].description for cmd in cmd_names]
        return self.find_matches(word_before_cursor, cmd_names, mode='strict',
                                 meta_collection=desc)

    def get_datatype_matches(self, suggestion, word_before_cursor):
        # suggest custom datatypes
        types = self.populate_schema_objects(suggestion.schema, 'datatypes')
        matches = self.find_matches(word_before_cursor, types, meta='datatype')

        if not suggestion.schema:
            # Also suggest hardcoded types
            matches.extend(self.find_matches(word_before_cursor, self.datatypes,
                                             mode='strict', meta='datatype'))

        return matches

    def get_namedquery_matches(self, _, word_before_cursor):
        return self.find_matches(
            word_before_cursor, NamedQueries.instance.list(), meta='named query')

    suggestion_matchers = {
        Column: get_column_matches,
        Function: get_function_matches,
        Schema: get_schema_matches,
        Table: get_table_matches,
        View: get_view_matches,
        Alias: get_alias_matches,
        Database: get_database_matches,
        Keyword: get_keyword_matches,
        Special: get_special_matches,
        Datatype: get_datatype_matches,
        NamedQuery: get_namedquery_matches,
    }

    def populate_scoped_cols(self, scoped_tbls):
        """ Find all columns in a set of scoped_tables
        :param scoped_tbls: list of TableReference namedtuples
        :return: list of column names
        """

        columns = []
        meta = self.dbmetadata

        for tbl in scoped_tbls:
            if tbl.schema:
                # A fully qualified schema.relname reference
                schema = self.escape_name(tbl.schema)
                relname = self.escape_name(tbl.name)

                if tbl.is_function:
                    # Return column names from a set-returning function
                    try:
                        # Get an array of FunctionMetadata objects
                        functions = meta['functions'][schema][relname]
                    except KeyError:
                        # No such function name
                        continue

                    for func in functions:
                        # func is a FunctionMetadata object
                        columns.extend(func.fieldnames())
                else:
                    # We don't know if schema.relname is a table or view. Since
                    # tables and views cannot share the same name, we can check
                    # one at a time
                    try:
                        columns.extend(meta['tables'][schema][relname])

                        # Table exists, so don't bother checking for a view
                        continue
                    except KeyError:
                        pass

                    try:
                        columns.extend(meta['views'][schema][relname])
                    except KeyError:
                        pass

            else:
                # Schema not specified, so traverse the search path looking for
                # a table or view that matches. Note that in order to get proper
                # shadowing behavior, we need to check both views and tables for
                # each schema before checking the next schema
                for schema in self.search_path:
                    relname = self.escape_name(tbl.name)

                    if tbl.is_function:
                        try:
                            functions = meta['functions'][schema][relname]
                        except KeyError:
                            continue

                        for func in functions:
                            # func is a FunctionMetadata object
                            columns.extend(func.fieldnames())
                    else:
                        try:
                            columns.extend(meta['tables'][schema][relname])
                            break
                        except KeyError:
                            pass

                        try:
                            columns.extend(meta['views'][schema][relname])
                            break
                        except KeyError:
                            pass

        return columns

    def populate_schema_objects(self, schema, obj_type):
        """Returns list of tables or functions for a (optional) schema"""

        metadata = self.dbmetadata[obj_type]

        if schema:
            try:
                objects = metadata[schema].keys()
            except KeyError:
                # schema doesn't exist
                objects = []
        else:
            schemas = self.search_path
            objects = [obj for schema in schemas
                           for obj in metadata[schema].keys()]

        return objects

    def populate_functions(self, schema, filter_func):
        """Returns a list of function names

        filter_func is a function that accepts a FunctionMetadata namedtuple
        and returns a boolean indicating whether that function should be
        kept or discarded
        """

        metadata = self.dbmetadata['functions']

        # Because of multiple dispatch, we can have multiple functions
        # with the same name, which is why `for meta in metas` is necessary
        # in the comprehensions below
        if schema:
            try:
                return [func for (func, metas) in metadata[schema].items()
                                for meta in metas
                                    if filter_func(meta)]
            except KeyError:
                return []
        else:
            return [func for schema in self.search_path
                            for (func, metas) in metadata[schema].items()
                                for meta in metas
                                    if filter_func(meta)]



