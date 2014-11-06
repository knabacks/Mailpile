# These are the Mailpile commands, the public "API" we expose for searching,
# tagging and editing e-mail.
#
import copy
import datetime
import json
import os
import os.path
import random
import re
import shlex
import socket
import sys
import traceback
import threading
import time
import webbrowser

import mailpile.util
import mailpile.ui
import mailpile.postinglist
from mailpile.crypto.gpgi import GnuPG
from mailpile.eventlog import Event
from mailpile.i18n import gettext as _
from mailpile.i18n import ngettext as _n
from mailpile.mailboxes import IsMailbox
from mailpile.mailutils import AddressHeaderParser, ClearParseCache
from mailpile.mailutils import ExtractEmails, ExtractEmailAndName, Email
from mailpile.postinglist import GlobalPostingList
from mailpile.safe_popen import MakePopenUnsafe, MakePopenSafe
from mailpile.search import MailIndex
from mailpile.util import *
from mailpile.vcard import AddressInfo


class Command(object):
    """Generic command object all others inherit from"""
    SYNOPSIS = (None,     # CLI shortcode, e.g. A:
                None,     # CLI shortname, e.g. add
                None,     # API endpoint, e.g. sys/addmailbox
                None)     # Positional argument list
    SYNOPSIS_ARGS = None  # New-style positional argument list
    API_VERSION = None
    UI_CONTEXT = None
    IS_USER_ACTIVITY = False
    IS_HANGING_ACTIVITY = False
    IS_INTERACTIVE = False
    CONFIG_REQUIRED = True

    COMMAND_CACHE_TTL = 0   # < 1 = Not cached
    CHANGES_SESSION_CONTEXT = False

    FAILURE = 'Failed: %(name)s %(args)s'
    ORDER = (None, 0)
    SPLIT_ARG = True  # Uses shlex by default
    RAISES = (UsageError, UrlRedirectException)
    WITH_CONTEXT = ()

    # Event logging settings
    LOG_NOTHING = False
    LOG_ARGUMENTS = True
    LOG_PROGRESS = False
    LOG_STARTING = '%(name)s: Starting'
    LOG_FINISHED = '%(name)s: %(message)s'

    # HTTP settings (note: security!)
    HTTP_CALLABLE = ('GET', )
    HTTP_POST_VARS = {}
    HTTP_QUERY_VARS = {}
    HTTP_BANNED_VARS = {}
    HTTP_STRICT_VARS = True
    HTTP_AUTH_REQUIRED = True

    class CommandResult:
        def __init__(self, command_obj, session,
                     command_name, doc, result, status, message,
                     template_id=None, kwargs={}, error_info={}):
            self.session = session
            self.command_obj = command_obj
            self.command_name = command_name
            self.kwargs = {}
            self.kwargs.update(kwargs)
            self.template_id = template_id
            self.doc = doc
            self.result = result
            self.status = status
            self.error_info = {}
            self.error_info.update(error_info)
            self.message = message
            self.rendered = {}
            self.renderers = {
                'json': self.as_json,
                'html': self.as_html,
                'text': self.as_text,
                'css': self.as_css,
                'rss': self.as_rss,
                'xml': self.as_xml,
                'txt': self.as_txt,
                'js': self.as_js
            }

        def __nonzero__(self):
            return (self.result and True or False)

        def as_(self, what, *args, **kwargs):
            if args or kwargs:
                # Args render things un-cacheable.
                return self.renderers.get(what)(*args, **kwargs)

            if what not in self.rendered:
                self.rendered[what] = self.renderers.get(what, self.as_text)()
            return self.rendered[what]

        def as_text(self):
            if isinstance(self.result, bool):
                happy = '%s: %s' % (self.result and _('OK') or _('Failed'),
                                    self.message or self.doc)
                if not self.result and self.error_info:
                    return '%s\n%s' % (happy,
                        json.dumps(self.error_info, indent=4,
                                   default=mailpile.util.json_helper))
                else:
                    return happy
            elif isinstance(self.result, (dict, list, tuple)):
                return json.dumps(self.result, indent=4, sort_keys=True,
                    default=mailpile.util.json_helper)
            else:
                return unicode(self.result)

        __str__ = lambda self: self.as_text()

        __unicode__ = lambda self: self.as_text()

        def as_dict(self):
            from mailpile.urlmap import UrlMap
            rv = {
                'command': self.command_name,
                'state': {
                    'command_url': UrlMap.ui_url(self.command_obj),
                    'context_url': UrlMap.context_url(self.command_obj),
                    'query_args': self.command_obj.state_as_query_args(),
                    'cache_id': self.command_obj.cache_id(),
                    'context': self.command_obj.context or ''
                },
                'status': self.status,
                'message': self.message,
                'result': self.result,
                'event_id': self.command_obj.event.event_id,
                'elapsed': '%.3f' % self.session.ui.time_elapsed,
            }
            if self.error_info:
                rv['error'] = self.error_info
            for ui_key in [k for k in self.kwargs.keys()
                           if k.startswith('ui_')]:
                rv[ui_key] = self.kwargs[ui_key]
            return rv

        def as_json(self):
            return self.session.ui.render_json(self.as_dict())

        def as_html(self, template=None):
            return self.as_template('html', template)

        def as_js(self, template=None):
            return self.as_template('js', template)

        def as_css(self, template=None):
            return self.as_template('css', template)

        def as_rss(self, template=None):
            return self.as_template('rss', template)

        def as_xml(self, template=None):
            return self.as_template('xml', template)

        def as_txt(self, template=None):
            return self.as_template('txt', template)

        def as_template(self, etype, template=None):
            what = ''.join((etype, '/' if template else '', template or ''))
            for e in ('jhtml', 'jjs', 'jcss', 'jxml', 'jrss'):
                if self.session.ui.render_mode.endswith(e):
                    what += ':content'
            if what in self.rendered:
                return self.rendered[what]

            tpath = self.command_obj.template_path(
                etype, template_id=self.template_id, template=template)

            data = self.as_dict()
            data['title'] = self.message

            def render():
                return self.session.ui.render_web(
                    self.session.config, [tpath], data)

            if what.endswith(':content'):
                data['render_mode'] = 'content'
                data['result'] = render()
                self.rendered[what] = self.session.ui.render_json(data)
            else:
                data['render_mode'] = 'full'
                self.rendered[what] = render()

            return self.rendered[what]

    def __init__(self, session, name=None, arg=None, data=None, async=False):
        self.session = session
        self.context = None
        self.name = self.SYNOPSIS[1] or self.SYNOPSIS[2] or name
        self.data = data or {}
        self.status = 'unknown'
        self.message = name
        self.error_info = {}
        self.result = None
        self.run_async = async
        if type(arg) in (type(list()), type(tuple())):
            self.args = tuple(arg)
        elif arg:
            if self.SPLIT_ARG is True:
                try:
                    self.args = tuple([a.decode('utf-8') for a in
                                       shlex.split(arg.encode('utf-8'))])
                except (ValueError, UnicodeEncodeError, UnicodeDecodeError):
                    raise UsageError(_('Failed to parse arguments'))
            else:
                self.args = (arg, )
        else:
            self.args = tuple([])
        if 'arg' in self.data:
            self.args = tuple(list(self.args) + self.data['arg'])
        self._create_event()

    def state_as_query_args(self):
        args = {}
        if self.args:
            args['arg'] = self._sloppy_copy(self.args)
        args.update(self._sloppy_copy(self.data))
        return args

    def cache_id(self, sqa=None):
        if self.COMMAND_CACHE_TTL < 1:
            return ''
        from mailpile.urlmap import UrlMap
        args = sorted(list((sqa or self.state_as_query_args()).iteritems()))
        # The replace() stuff makes these usable as CSS class IDs
        return ('%s-%s' % (UrlMap.ui_url(self), md5_hex(str(args)))
                          ).replace('/', '-').replace('.', '-')

    def cache_requirements(self, result):
        raise NotImplementedError('Cachable commands should override this, '
                                  'returning a set() of requirements.')

    def cache_result(self, result):
        if self.COMMAND_CACHE_TTL > 0:
            cache_id = self.cache_id()
            if cache_id:
                self.session.config.command_cache.cache_result(
                    cache_id,
                    time.time() + self.COMMAND_CACHE_TTL,
                    self.cache_requirements(result),
                    self,
                    result)
                self.session.ui.mark(_('Cached result as %s') % cache_id)

    def template_path(self, etype, template_id=None, template=None):
        path_parts = (template_id or self.SYNOPSIS[2] or 'command').split('/')
        if len(path_parts) == 1:
            path_parts.append('index')
        if template not in (None, etype, 'as.' + etype):
            # Security: The template request may come from the URL, so we
            #           sanitize it very aggressively before heading off
            #           to the filesystem.
            clean_tpl = CleanText(template.replace('.%s' % etype, ''),
                                  banned=(CleanText.FS +
                                          CleanText.WHITESPACE))
            path_parts[-1] += '-%s' % clean_tpl
        path_parts[-1] += '.' + etype
        return os.path.join(*path_parts)

    def _gnupg(self):
        return GnuPG(self.session.config)

    def _config(self):
        session, config = self.session, self.session.config
        if not config.loaded_config:
            config.load(session)
            parent = session
            config.prepare_workers(session, daemons=self.IS_INTERACTIVE)
        if self.IS_INTERACTIVE and not config.daemons_started():
            config.prepare_workers(session, daemons=True)
        return config

    def _idx(self, reset=False, wait=True, wait_all=True, quiet=False):
        session, config = self.session, self._config()

        if not reset and config.index:
            return config.index

        def __do_load2():
            config.vcards.load_vcards(session)
            if not wait_all:
                session.ui.report_marks(quiet=quiet)

        def __do_load1():
            if reset:
                config.index = None
                session.results = []
                session.searched = []
                session.displayed = None
            idx = config.get_index(session)
            if wait_all:
                __do_load2()
            if not wait:
                session.ui.report_marks(quiet=quiet)
            return idx

        if wait:
            rv = config.save_worker.do(session, 'Load', __do_load1)
            session.ui.reset_marks(quiet=quiet)
        else:
            config.save_worker.add_task(session, 'Load', __do_load1)
            rv = None

        if not wait_all:
            config.save_worker.add_task(session, 'Load2', __do_load2)

        return rv

    def _background_save(self,
                         everything=False, config=False,
                         index=False, index_full=False,
                         wait=False, wait_callback=None):
        session, cfg = self.session, self.session.config
        aut = cfg.save_worker.add_unique_task
        if everything or config:
            aut(session, 'Save config', cfg.save)
        if cfg.index:
            cfg.flush_mbox_cache(session, clear=False, wait=wait)
            if index_full:
                aut(session, 'Save index', lambda: self._idx().save(session))
            elif everything or index:
                aut(session, 'Save index changes',
                    lambda: self._idx().save_changes(session))
        if wait:
            wait_callback = wait_callback or (lambda: True)
            cfg.save_worker.do(session, 'Waiting', wait_callback)

    def _choose_messages(self, words, allow_ephemeral=False):
        msg_ids = set()
        all_words = []
        for word in words:
            all_words.extend(word.split(','))
        for what in all_words:
            if what.lower() == 'these':
                if self.session.displayed:
                    b = self.session.displayed['stats']['start'] - 1
                    c = self.session.displayed['stats']['count']
                    msg_ids |= set(self.session.results[b:b + c])
                else:
                    self.session.ui.warning(_('No results to choose from!'))
            elif what.lower() == 'all':
                if self.session.results:
                    msg_ids |= set(self.session.results)
                else:
                    self.session.ui.warning(_('No results to choose from!'))
            elif what.startswith('='):
                try:
                    msg_id = int(what[1:], 36)
                    if msg_id >= 0 and msg_id < len(self._idx().INDEX):
                        msg_ids.add(msg_id)
                    else:
                        self.session.ui.warning((_('No such ID: %s')
                                                 ) % (what[1:], ))
                except ValueError:
                    if allow_ephemeral and '-' in what:
                        msg_ids.add(what[1:])
                    else:
                        self.session.ui.warning(_('What message is %s?'
                                                  ) % (what, ))
            elif '-' in what:
                try:
                    b, e = what.split('-')
                    msg_ids |= set(self.session.results[int(b) - 1:int(e)])
                except (ValueError, KeyError, IndexError, TypeError):
                    self.session.ui.warning(_('What message is %s?'
                                              ) % (what, ))
            else:
                try:
                    msg_ids.add(self.session.results[int(what) - 1])
                except (ValueError, KeyError, IndexError, TypeError):
                    self.session.ui.warning(_('What message is %s?'
                                              ) % (what, ))
        return msg_ids

    def _error(self, message, info=None, result=None):
        self.status = 'error'
        self.message = message

        ui_message = _('%s error: %s') % (self.name, message)
        if info:
            self.error_info.update(info)
            details = ' '.join(['%s=%s' % (k, info[k]) for k in info])
            ui_message += ' (%s)' % details
        self.session.ui.mark(self.name)
        self.session.ui.error(ui_message)

        if result:
            return self.view(result)
        else:
            return False

    def _success(self, message, result=True):
        self.status = 'success'
        self.message = message

        ui_message = '%s: %s' % (self.name, message)
        self.session.ui.mark(ui_message)

        return self.view(result)

    def _read_file_or_data(self, fn):
        if fn in self.data:
            return self.data[fn]
        else:
            return open(fn, 'rb').read()

    def _ignore_exception(self):
        self.session.ui.debug(traceback.format_exc())

    def _serialize(self, name, function):
        return function()

    def _background(self, name, function):
        session, config = self.session, self.session.config
        return config.slow_worker.add_task(session, name, function)

    def _update_event_state(self, state, log=False):
        self.event.flags = state
        self.event.data['elapsed'] = int(1000 * (time.time()-self._start_time))

        if (log or self.LOG_PROGRESS) and not self.LOG_NOTHING:
            ui = str(self.session.ui.__class__).replace('mailpile.', '.')
            self.event.data['ui'] = ui
            self.event.data['output'] = self.session.ui.render_mode
            if self.session.config.event_log:
                self.session.config.event_log.log_event(self.event)

    def _starting(self):
        self._start_time = time.time()
        self._update_event_state(Event.RUNNING)
        if self.name:
            self.session.ui.start_command(self.name, self.args, self.data)

    def _fmt_msg(self, message):
        return message % {'name': self.name,
                          'status': self.status or '',
                          'message': self.message or ''}

    def _sloppy_copy(self, data, name=None):
        if name and 'pass' == name[:4]:
            data = '(SUPPRESSED)'
        def copy_value(v):
            try:
                unicode(v).encode('utf-8')
                return unicode(v)[:1024]
            except (UnicodeEncodeError, UnicodeDecodeError):
                return '(BINARY DATA)'
        if isinstance(data, (list, tuple)):
            return [self._sloppy_copy(i, name=name) for i in data]
        elif isinstance(data, dict):
            return dict((k, self._sloppy_copy(v, name=k))
                        for k, v in data.iteritems())
        else:
            return copy_value(data)

    def _create_event(self):
        private_data = {}
        if self.LOG_ARGUMENTS:
            if self.data:
                private_data['data'] = self._sloppy_copy(self.data)
            if self.args:
                private_data['args'] = self._sloppy_copy(self.args)

        self.event = self._make_command_event(private_data)

    def _make_command_event(self, private_data):
        return Event(source=self,
                     message=self._fmt_msg(self.LOG_STARTING),
                     flags=Event.INCOMPLETE,
                     data={},
                     private_data=private_data)

    def _finishing(self, command, rv, just_cleanup=False):
        if just_cleanup:
            self._update_finished_event()
            return rv

        if not self.context:
            self.context = self.session.get_context(
                update=self.CHANGES_SESSION_CONTEXT)

        self.session.ui.mark(_('Generating result'))
        result = self.CommandResult(self, self.session, self.name,
                                    command.__doc__ or self.__doc__,
                                    rv, self.status, self.message,
                                    error_info=self.error_info)
        self.cache_result(result)

        if not self.run_async:
            self._update_finished_event()
        return result

    def _update_finished_event(self):
        # Update the event!
        if self.message:
            self.event.message = self.message
        if self.error_info:
            self.event.private_data['error_info'] = self.error_info
        self.event.message = self._fmt_msg(self.LOG_FINISHED)
        self._update_event_state(Event.COMPLETE, log=True)

        self.session.ui.mark(self.event.message)
        self.session.ui.report_marks(
            details=('timing' in self.session.config.sys.debug))
        if self.name:
            self.session.ui.finish_command(self.name)

    def _run_sync(self, enable_cache, *args, **kwargs):
        self._starting()
        self._run_args = args
        self._run_kwargs = kwargs

        if (self.COMMAND_CACHE_TTL > 0 and
               'http' not in self.session.config.sys.debug and
               enable_cache):
            cid = self.cache_id()
            try:
                rv = self.session.config.command_cache.get_result(cid)
                rv.session.ui = self.session.ui
                if self.CHANGES_SESSION_CONTEXT:
                    self.session.copy(rv.session, ui=False)
                self.session.ui.mark(_('Using pre-cached result object %s') % cid)
                self._finishing(self, True, just_cleanup=True)
                return rv
            except:
                pass

        def command(self, *args, **kwargs):
            if self.CONFIG_REQUIRED:
                if not self.session.config.loaded_config:
                    return self._error(_('Please log in'))
                if mailpile.util.QUITTING:
                    return self._error(_('Shutting down'))
            return self.command(*args, **kwargs)

        try:
            return self._finishing(command, command(self, *args, **kwargs))
        except self.RAISES:
            self.status = 'success'
            self._finishing(command, True, just_cleanup=True)
            raise
        except:
            self._ignore_exception()
            self._error(self.FAILURE % {'name': self.name,
                                        'args': ' '.join(self.args)})
            return self._finishing(command, False)

    def _run(self, *args, **kwargs):
        if self.run_async:
            def streetcar():
                try:
                    with MultiContext(self.WITH_CONTEXT):
                        rv = self._run_sync(True, *args, **kwargs).as_dict()
                        self.event.private_data.update(rv)
                        self._update_finished_event()
                except:
                    traceback.print_exc()

            self._starting()
            self._update_event_state(self.event.RUNNING, log=True)
            result = Command.CommandResult(self, self.session, self.name,
                                           self.__doc__,
                                           {"resultid": self.event.event_id},
                                           "success",
                                           "Running in background")

            self.session.config.async_worker.add_task(self.session, self.name,
                                                      streetcar)
            return result

        else:
            return self._run_sync(True, *args, **kwargs)

    def run(self, *args, **kwargs):
        with MultiContext(self.WITH_CONTEXT):
            if self.IS_USER_ACTIVITY:
                try:
                    mailpile.util.LAST_USER_ACTIVITY = time.time()
                    mailpile.util.LIVE_USER_ACTIVITIES += 1
                    return self._run(*args, **kwargs)
                finally:
                    mailpile.util.LIVE_USER_ACTIVITIES -= 1
            else:
                return self._run(*args, **kwargs)

    def refresh(self):
        self._create_event()
        return self._run_sync(False, *self._run_args, **self._run_kwargs)

    def command(self):
        return None

    def etag_data(self):
        return []

    def max_age(self):
        return 0

    @classmethod
    def view(cls, result):
        return result


##[ Shared basic Search Result class]#########################################

class SearchResults(dict):

    _NAME_TITLES = ('the', 'mr', 'ms', 'mrs', 'sir', 'dr', 'lord')

    def _name(self, sender, short=True, full_email=False):
        words = re.sub('["<>]', '', sender).split()
        nomail = [w for w in words if not '@' in w]
        if nomail:
            if short:
                if len(nomail) > 1 and nomail[0].lower() in self._NAME_TITLES:
                    return nomail[1]
                return nomail[0]
            return ' '.join(nomail)
        elif words:
            if not full_email:
                return words[0].split('@', 1)[0]
            return words[0]
        return '(nobody)'

    def _names(self, senders):
        if len(senders) > 1:
            names = {}
            for sender in senders:
                sname = self._name(sender)
                names[sname] = names.get(sname, 0) + 1
            namelist = names.keys()
            namelist.sort(key=lambda n: -names[n])
            return ', '.join(namelist)
        if len(senders) < 1:
            return '(no sender)'
        if senders:
            return self._name(senders[0], short=False)
        return ''

    def _compact(self, namelist, maxlen):
        l = len(namelist)
        while l > maxlen:
            namelist = re.sub(', *[^, \.]+, *', ',,', namelist, 1)
            if l == len(namelist):
                break
            l = len(namelist)
        namelist = re.sub(',,,+, *', ' .. ', namelist, 1)
        return namelist

    TAG_TYPE_FLAG_MAP = {
        'trash': 'trash',
        'spam': 'spam',
        'ham': 'ham',
        'drafts': 'draft',
        'blank': 'draft',
        'sent': 'from_me',
        'outbox': 'from_me',
        'replied': 'replied',
        'fwded': 'forwarded'
    }

    def _metadata(self, msg_info):
        import mailpile.urlmap
        nz = lambda l: [v for v in l if v]
        msg_ts = long(msg_info[MailIndex.MSG_DATE], 36)
        msg_date = datetime.datetime.fromtimestamp(msg_ts)

        fe, fn = ExtractEmailAndName(msg_info[MailIndex.MSG_FROM])
        f_info = self._address(e=fe, n=fn)
        f_info['aid'] = (self._msg_addresses(msg_info, no_to=True, no_cc=True)
                         or [''])[0]
        expl = {
            'mid': msg_info[MailIndex.MSG_MID],
            'id': msg_info[MailIndex.MSG_ID],
            'timestamp': msg_ts,
            'from': f_info,
            'to_aids': self._msg_addresses(msg_info, no_from=True, no_cc=True),
            'cc_aids': self._msg_addresses(msg_info, no_from=True, no_to=True),
            'msg_kb': int(msg_info[MailIndex.MSG_KB], 36),
            'tag_tids': sorted(self._msg_tags(msg_info)),
            'thread_mid': msg_info[MailIndex.MSG_THREAD_MID],
            'subject': msg_info[MailIndex.MSG_SUBJECT],
            'body': MailIndex.get_body(msg_info),
            'flags': {
            },
            'crypto': {
            }
        }

        # Ephemeral messages do not have URLs
        if '-' in msg_info[MailIndex.MSG_MID]:
            expl['flags'].update({
                'ephemeral': True,
                'draft': True,
            })
        else:
            expl['urls'] = {
                'thread': self.urlmap.url_thread(msg_info[MailIndex.MSG_MID]),
                'source': self.urlmap.url_source(msg_info[MailIndex.MSG_MID]),
            }

        # Support rich snippets
        if expl['body']['snippet'].startswith('{'):
            try:
                expl['body'] = json.loads(expl['body']['snippet'])
            except ValueError:
                pass

        # Misc flags
        sender_vcard = self.idx.config.vcards.get_vcard(fe.lower())
        if sender_vcard:
            if sender_vcard.kind == 'profile':
                expl['flags']['from_me'] = True
        tag_types = [self.idx.config.get_tag(t).type for t in expl['tag_tids']]
        for t in self.TAG_TYPE_FLAG_MAP:
            if t in tag_types:
                expl['flags'][self.TAG_TYPE_FLAG_MAP[t]] = True

        # Check tags for signs of encryption or signatures
        tag_slugs = [self.idx.config.get_tag(t).slug for t in expl['tag_tids']]
        for t in tag_slugs:
            if t.startswith('mp_sig'):
                expl['crypto']['signature'] = t[7:]
            elif t.startswith('mp_enc'):
                expl['crypto']['encryption'] = t[7:]

        # Extra behavior for editable messages
        if 'draft' in expl['flags']:
            if 'ephemeral' in expl['flags']:
                pass
            elif self.idx.config.is_editable_message(msg_info):
                expl['urls']['editing'] = self.urlmap.url_edit(expl['mid'])
            else:
                del expl['flags']['draft']

        return expl

    def _msg_addresses(self, msg_info=None, addresses=[],
                       no_from=False, no_to=False, no_cc=False):
        cids = set()

        for ai in addresses:
            try:
                cids.add(b36(self.idx.EMAIL_IDS[ai.address.lower()]))
            except KeyError:
                cids.add(b36(self.idx._add_email(ai.address, name=ai.fn)))

        if msg_info:
            if not no_to:
                to = [t for t in msg_info[MailIndex.MSG_TO].split(',') if t]
                cids |= set(to)
            if not no_cc:
                cc = [t for t in msg_info[MailIndex.MSG_CC].split(',') if t]
                cids |= set(cc)
            if not no_from:
                fe, fn = ExtractEmailAndName(msg_info[MailIndex.MSG_FROM])
                if fe:
                    try:
                        cids.add(b36(self.idx.EMAIL_IDS[fe.lower()]))
                    except KeyError:
                        cids.add(b36(self.idx._add_email(fe, name=fn)))

        return sorted(list(cids))

    def _address(self, cid=None, e=None, n=None):
        if cid and not (e and n):
            e, n = ExtractEmailAndName(self.idx.EMAILS[int(cid, 36)])
        vcard = self.session.config.vcards.get_vcard(e)
        if vcard and '@' in n:
            n = vcard.fn
        return AddressInfo(e, n, vcard=vcard)

    def _msg_tags(self, msg_info):
        tids = [t for t in msg_info[MailIndex.MSG_TAGS].split(',')
                if t and t in self.session.config.tags]
        return tids

    def _tag(self, tid, attributes={}):
        return dict_merge(self.session.config.get_tag_info(tid), attributes)

    def _thread(self, thread_mid):
        msg_info = self.idx.get_msg_at_idx_pos(int(thread_mid, 36))
        thread = [i for i in msg_info[MailIndex.MSG_REPLIES].split(',') if i]

        # FIXME: This is a hack, the indexer should just keep things
        #        in the right order on rescan. Fixing threading is a bigger
        #        problem though, so we do this for now.
        def thread_sort_key(idx):
            info = self.idx.get_msg_at_idx_pos(int(thread_mid, 36))
            return int(info[self.idx.MSG_DATE], 36)
        thread.sort(key=thread_sort_key)

        return thread

    WANT_MSG_TREE = ('attachments', 'html_parts', 'text_parts', 'header_list',
                     'editing_strings', 'crypto')
    PRUNE_MSG_TREE = ('headers', )  # Added by editing_strings

    def _prune_msg_tree(self, tree):
        for k in tree.keys():
            if k not in self.WANT_MSG_TREE or k in self.PRUNE_MSG_TREE:
                del tree[k]
        return tree

    def _message(self, email):
        tree = email.get_message_tree(want=(email.WANT_MSG_TREE_PGP +
                                            self.WANT_MSG_TREE))
        email.evaluate_pgp(tree, decrypt=True)

        editing_strings = tree.get('editing_strings')
        if editing_strings:
            for key in ('from', 'to', 'cc', 'bcc'):
                if key in editing_strings:
                    cids = self._msg_addresses(
                        addresses=AddressHeaderParser(
                            unicode_data=editing_strings[key]))
                    editing_strings['%s_aids' % key] = cids
                    for cid in cids:
                        if cid not in self['data']['addresses']:
                            self['data']['addresses'
                                         ][cid] = self._address(cid=cid)

        return self._prune_msg_tree(tree)

    def __init__(self, session, idx,
                 results=None, start=0, end=None, num=None,
                 emails=None, people=None,
                 suppress_data=False, full_threads=True):
        dict.__init__(self)
        self.session = session
        self.people = people
        self.emails = emails
        self.idx = idx
        self.urlmap = mailpile.urlmap.UrlMap(self.session)

        results = self.results = results or session.results or []

        num = num or session.config.prefs.num_results
        if end:
            start = end - num
        if start > len(results):
            start = len(results)
        if start < 0:
            start = 0

        try:
            threads = [b36(r) for r in results[start:start + num]]
        except TypeError:
            results = threads = []
            start = end = 0

        self.session.ui.mark(_('Parsing metadata for %d results '
                               '(full_threads=%s)') % (len(threads),
                                                       full_threads))

        self.update({
            'summary': _('Search: %s') % ' '.join(session.searched),
            'stats': {
                'count': len(threads),
                'start': start + 1,
                'end': start + min(num, len(results)-start),
                'total': len(results),
            },
            'search_terms': session.searched,
            'address_ids': [],
            'message_ids': [],
            'thread_ids': threads,
        })
        if 'tags' in self.session.config:
            search_tags = [idx.config.get_tag(t.split(':')[1], {})
                           for t in session.searched
                           if t.startswith('in:') or t.startswith('tag:')]
            search_tag_ids = [t._key for t in search_tags if t]
            self.update({
                'search_tag_ids': search_tag_ids,
            })
            if search_tag_ids:
                self['summary'] = ' & '.join([t.name for t
                                              in search_tags if t])
        else:
            search_tag_ids = []

        if suppress_data or (not results and not emails):
            return

        self.update({
            'data': {
                'addresses': {},
                'metadata': {},
                'messages': {},
                'threads': {}
            }
        })
        if 'tags' in self.session.config:
            th = self['data']['tags'] = {}
            for tid in search_tag_ids:
                if tid not in th:
                    th[tid] = self._tag(tid, {'searched': True})

        idxs = results[start:start + num]
        while idxs:
            idx_pos = idxs.pop(0)
            msg_info = idx.get_msg_at_idx_pos(idx_pos)
            self.add_msg_info(b36(idx_pos), msg_info,
                              full_threads=full_threads, idxs=idxs)

        if emails and len(emails) == 1:
            self['summary'] = emails[0].get_msg_info(MailIndex.MSG_SUBJECT)

        for e in emails or []:
            self.add_email(e)

    def add_msg_info(self, mid, msg_info, full_threads=False, idxs=None):
        # Populate data.metadata
        self['data']['metadata'][mid] = self._metadata(msg_info)

        # Populate data.thread
        thread_mid = msg_info[self.idx.MSG_THREAD_MID]
        if thread_mid not in self['data']['threads']:
            thread = self._thread(thread_mid)
            self['data']['threads'][thread_mid] = thread
            if full_threads and idxs:
                idxs.extend([int(t, 36) for t in thread
                             if t not in self['data']['metadata']])

        # Populate data.person
        for cid in self._msg_addresses(msg_info):
            if cid not in self['data']['addresses']:
                self['data']['addresses'][cid] = self._address(cid=cid)

        # Populate data.tag
        if 'tags' in self.session.config:
            for tid in self._msg_tags(msg_info):
                if tid not in self['data']['tags']:
                    self['data']['tags'][tid] = self._tag(tid,
                                                          {"searched": False})

    def add_email(self, e):
        if e not in self.emails:
            self.emails.append(e)
        mid = e.msg_mid()
        if mid not in self['data']['messages']:
            self['data']['messages'][mid] = self._message(e)
        if mid not in self['message_ids']:
            self['message_ids'].append(mid)
        # This happens last, as the parsing above may have side-effects
        # which matter once we get this far.
        self.add_msg_info(mid, e.get_msg_info(uncached=True))

    def __nonzero__(self):
        return True

    def next_set(self):
        stats = self['stats']
        return SearchResults(self.session, self.idx,
                             start=stats['start'] - 1 + stats['count'])

    def previous_set(self):
        stats = self['stats']
        return SearchResults(self.session, self.idx,
                             end=stats['start'] - 1)

    def as_text(self):
        from mailpile.jinjaextensions import MailpileCommand as JE
        clen = max(3, len('%d' % len(self.session.results)))
        cfmt = '%%%d.%ds' % (clen, clen)

        term_width = self.session.ui.term.max_width()
        fs_width = int((22 + 53) * (term_width / 79.0))
        f_width = min(32, int(0.30 * fs_width))
        s_width = fs_width - f_width

        text = []
        count = self['stats']['start']
        expand_ids = [e.msg_idx_pos for e in (self.emails or [])]
        addresses = self.get('data', {}).get('addresses', {})

        for mid in self['thread_ids']:
            m = self['data']['metadata'][mid]
            tags = [self['data']['tags'][t] for t in m['tag_tids']]
            tag_names = [t['name'] for t in tags
                         if not t.get('searched', False)
                         and t.get('label', True)
                         and t.get('display', '') != 'invisible']
            tag_new = [t for t in tags if t.get('type') == 'unread']
            tag_names.sort()
            msg_meta = tag_names and ('  (' + '('.join(tag_names)) or ''

            # FIXME: this is a bit ugly, but useful for development
            es = ['', '']
            for t in [t['slug'] for t in tags]:
                if t.startswith('mp_enc') and 'none' not in t:
                    es[1] = 'E'
                if t.startswith('mp_sig') and 'none' not in t:
                    es[0] = 'S'
            es = ''.join([e for e in es if e])
            if es:
                msg_meta = (msg_meta or '  ') + ('[%s]' % es)
            elif msg_meta:
                msg_meta += ')'
            else:
                msg_meta += '  '
            msg_meta += elapsed_datetime(m['timestamp'])

            from_info = (m['from'].get('fn') or m['from'].get('email')
                         or '(anonymous)')
            if from_info[:1] in ('<', '"', '\''):
                from_info = from_info[1:]
                if from_info[-1:] in ('>', '"', '\''):
                    from_info = from_info[:-1]
            if '@' in from_info and len(from_info) > 18:
                e, d = from_info.split('@', 1)
                if d in ('gmail.com', 'yahoo.com', 'hotmail.com'):
                    from_info = '%s@%s..' % (e, d[0])
                else:
                    from_info = '%s..@%s' % (e[0], d)

            if not expand_ids:
                def gg(pos):
                    return (pos < 10) and pos or '>'
                thread = [m['thread_mid']]
                thread += self['data']['threads'][m['thread_mid']]
                if m['mid'] not in thread:
                    thread.append(m['mid'])
                pos = thread.index(m['mid']) + 1
                if pos > 1:
                    from_info = '%s>%s' % (gg(pos-1), from_info)
                else:
                    from_info = '  ' + from_info
                if pos < len(thread):
                    from_info = '%s>%s' % (from_info[:20], gg(len(thread)-pos))

            subject = re.sub('^(\\[[^\\]]{6})[^\\]]{3,}\\]\\s*', '\\1..] ',
                             JE._nice_subject(m))

            sfmt = '%%-%d.%ds%%s' % (max(1, s_width - (clen + len(msg_meta))),
                                     max(1, s_width - (clen + len(msg_meta))))
            ffmt = ' %%-%d.%ds %%s' % (f_width, f_width)
            tfmt = cfmt + ffmt + sfmt
            text.append(tfmt % (count, from_info, tag_new and '*' or ' ',
                                subject, msg_meta))

            if mid in self['data'].get('messages', {}):
                exp_email = self.emails[expand_ids.index(int(mid, 36))]
                msg_tree = exp_email.get_message_tree()
                text.append('-' * term_width)
                text.append(exp_email.get_editing_string(msg_tree,
                    attachment_headers=False).strip())
                if msg_tree['attachments']:
                    text.append('\nAttachments:')
                    for a in msg_tree['attachments']:
                        text.append('%5.5s %s' % ('#%s' % a['count'],
                                                  a['filename']))
                text.append('-' * term_width)

            count += 1
        if not count:
            text = ['(No messages found)']
        return '\n'.join(text) + '\n'


##[ Internals ]###############################################################

class Load(Command):
    """Load or reload the metadata index"""
    SYNOPSIS = (None, 'load', None, None)
    ORDER = ('Internals', 1)
    CONFIG_REQUIRED = False
    IS_INTERACTIVE = True

    def command(self, reset=True, wait=True, wait_all=False, quiet=False):
        try:
            if self._idx(reset=reset,
                         wait=wait,
                         wait_all=wait_all,
                         quiet=quiet):
                return self._success(_('Loaded metadata index'))
            else:
                return self._error(_('Failed to loaded metadata index'))
        except IOError:
            return self._error(_('Failed to decrypt configuration, '
                                 'please log in!'))


class Rescan(Command):
    """Add new messages to index"""
    SYNOPSIS = (None, 'rescan', None,
                '[full|vcards|both|mailboxes|sources|<msgs>]')
    ORDER = ('Internals', 2)
    LOG_PROGRESS = True

    def command(self, slowly=False):
        session, config, idx = self.session, self.session.config, self._idx()
        args = list(self.args)

        # Pretend we're idle, to make rescan go fast fast.
        if not slowly:
            mailpile.util.LAST_USER_ACTIVITY = 0

        if args and args[0].lower() == 'vcards':
            return self._success(_('Rescanned vcards'),
                                 result=self._rescan_vcards(session))
        elif args and args[0].lower() in ('both', 'mailboxes', 'sources',
                                          'editable'):
            which = args[0].lower()
            return self._success(_('Rescanned mailboxes'),
                                 result=self._rescan_mailboxes(session,
                                                               which=which))
        elif args and args[0].lower() == 'full':
            config.flush_mbox_cache(session, wait=True)
            args.pop(0)

        # Clear the cache first, in case the user is flailing about
        ClearParseCache(full=True)

        msg_idxs = self._choose_messages(args)
        if msg_idxs:
            for msg_idx_pos in msg_idxs:
                e = Email(idx, msg_idx_pos)
                try:
                    session.ui.mark('Re-indexing %s' % e.msg_mid())
                    idx.index_email(self.session, e)
                except KeyboardInterrupt:
                    raise
                except:
                    self._ignore_exception()
                    session.ui.warning(_('Failed to reindex: %s'
                                         ) % e.msg_mid())

            self.event.data["messages"] = len(msg_idxs)
            self.session.config.event_log.log_event(self.event)
            self._background_save(index=True)

            return self._success(_('Indexed %d messages') % len(msg_idxs),
                                 result={'messages': len(msg_idxs)})

        else:
            # FIXME: Need a lock here?
            if 'rescan' in config._running:
                return self._success(_('Rescan already in progress'))
            config._running['rescan'] = True
            try:
                results = {}
                results.update(self._rescan_vcards(session))
                results.update(self._rescan_mailboxes(session))

                self.event.data.update(results)
                self.session.config.event_log.log_event(self.event)
                if 'aborted' in results:
                    raise KeyboardInterrupt()
                return self._success(_('Rescanned vcards and mailboxes'),
                                     result=results)
            except (KeyboardInterrupt), e:
                return self._error(_('User aborted'), info=results)
            finally:
                del config._running['rescan']

    def _rescan_vcards(self, session):
        from mailpile.plugins import PluginManager
        config = session.config
        imported = 0
        importer_cfgs = config.prefs.vcard.importers
        try:
            session.ui.mark(_('Rescanning: %s') % 'vcards')
            for importer in PluginManager.VCARD_IMPORTERS.values():
                for cfg in importer_cfgs.get(importer.SHORT_NAME, []):
                    if cfg:
                        imp = importer(session, cfg)
                        imported += imp.import_vcards(session, config.vcards)
                    if mailpile.util.QUITTING:
                        return {'vcards': imported, 'aborted': True}
        except KeyboardInterrupt:
            return {'vcards': imported, 'aborted': True}
        return {'vcards': imported}

    def _rescan_mailboxes(self, session, which='mailboxes'):
        import mailpile.mail_source
        config = session.config
        idx = self._idx()
        msg_count = 0
        mbox_count = 0
        rv = True
        try:
            session.ui.mark(_('Rescanning: %s') % which)

            pre_command = config.prefs.rescan_command
            if pre_command and not mailpile.util.QUITTING:
                session.ui.mark(_('Running: %s') % pre_command)
                try:
                    MakePopenUnsafe()
                    subprocess.check_call(pre_command, shell=True)
                finally:
                    MakePopenSafe()
            msg_count = 1

            if which in ('both', 'sources'):
                ocount = msg_count - 1
                while ocount != msg_count:
                    ocount = msg_count
                    sources = config.mail_sources.values()
                    sources.sort(key=lambda k: random.randint(0, 100))
                    for src in sources:
                        if mailpile.util.QUITTING:
                            ocount = msg_count
                            break
                        session.ui.mark(_('Rescanning: %s') % (src, ))
                        count = src.rescan_now(session)
                        if count > 0:
                            msg_count += count
                            mbox_count += 1
                        session.ui.mark('\n')

            if which in ('both', 'mailboxes', 'editable'):
                if which == 'editable':
                    mailboxes = config.get_mailboxes(mail_sources=True)
                else:
                    mailboxes = config.get_mailboxes(mail_sources=False)

                for fid, fpath, sc in mailboxes:
                    if mailpile.util.QUITTING:
                        break
                    if fpath == '/dev/null':
                        continue
                    try:
                        session.ui.mark(_('Rescanning: %s %s')
                                        % (fid, fpath))
                        if which == 'editable':
                            count = idx.scan_mailbox(session, fid, fpath,
                                                     config.open_mailbox,
                                                     process_new=False,
                                                     editable=True,
                                                     event=self.event)
                        else:
                            count = idx.scan_mailbox(session, fid, fpath,
                                                     config.open_mailbox,
                                                     event=self.event)
                    except ValueError:
                        self._ignore_exception()
                        count = -1
                    if count < 0:
                        session.ui.warning(_('Failed to rescan: %s') % fpath)
                    elif count > 0:
                        msg_count += count
                        mbox_count += 1
                    session.ui.mark('\n')

            msg_count -= 1
            session.ui.mark(_('Nothing changed'))
        except (KeyboardInterrupt, subprocess.CalledProcessError), e:
            return {'aborted': True,
                    'messages': msg_count,
                    'mailboxes': mbox_count}
        finally:
            if msg_count:
                session.ui.mark('\n')
                if msg_count < 500:
                    self._background_save(index=True)
                else:
                    self._background_save(index_full=True)
        return {'messages': msg_count,
                'mailboxes': mbox_count}


class Optimize(Command):
    """Optimize the keyword search index"""
    SYNOPSIS = (None, 'optimize', None, '[harder]')
    ORDER = ('Internals', 3)

    def command(self, slowly=False):
        try:
            if not slowly:
                mailpile.util.LAST_USER_ACTIVITY = 0
            self._idx().save(self.session)
            GlobalPostingList.Optimize(self.session, self._idx(),
                                       force=('harder' in self.args))
            return self._success(_('Optimized search engine'))
        except KeyboardInterrupt:
            return self._error(_('Aborted'))


class BrowseOrLaunch(Command):
    """Launch browser and exit, if already running"""
    SYNOPSIS = (None, 'browse_or_launch', None, None)
    ORDER = ('Internals', 5)
    CONFIG_REQUIRED = False
    RAISES = (KeyboardInterrupt,)

    @classmethod
    def Browse(cls, sspec):
        http_url = 'http://%s:%s/' % sspec
        try:
            MakePopenUnsafe()
            webbrowser.open(http_url)
            return http_url
        finally:
            MakePopenSafe()
        return False

    def command(self):
        config = self.session.config

        if config.http_worker:
            sspec = config.http_worker.sspec
        else:
            sspec = (config.sys.http_host, config.sys.http_port)

        try:
            socket.create_connection(sspec)
            self.Browse(sspec)
            os._exit(1)
        except IOError:
            pass

        return self._success(_('Launching Mailpile'), result=True)


class RunWWW(Command):
    """Just run the web server"""
    SYNOPSIS = (None, 'www', None, '[<host:port>]')
    ORDER = ('Internals', 5)
    CONFIG_REQUIRED = False

    def command(self):
        config = self.session.config

        if self.args:
            sspec = self.args[0].split(':', 1)
            sspec[1] = int(sspec[1])
        else:
            sspec = (config.sys.http_host, config.sys.http_port)

        self.session.config.prepare_workers(self.session,
                                            httpd_spec=tuple(sspec),
                                            daemons=True)
        if config.http_worker:
            http_url = 'http://%s:%s/' % config.http_worker.httpd.sspec
            return self._success(_('Started the web server on %s') % http_url)
        else:
            return self._error(_('Failed to started the web server'))


class WritePID(Command):
    """Write the PID to a file"""
    SYNOPSIS = (None, 'pidfile', None, "</path/to/pidfile>")
    ORDER = ('Internals', 5)
    CONFIG_REQUIRED = False
    SPLIT_ARG = False

    def command(self):
        with open(self.args[0], 'w') as fd:
            fd.write('%d' % os.getpid())
        return self._success(_('Wrote PID to %s') % self.args)


class RenderPage(Command):
    """Does nothing, for use by semi-static jinja2 pages"""
    SYNOPSIS = (None, None, 'page', None)
    ORDER = ('Internals', 6)
    CONFIG_REQUIRED = False
    SPLIT_ARG = False
    HTTP_STRICT_VARS = False
    IS_USER_ACTIVITY = True

    class CommandResult(Command.CommandResult):
        def __init__(self, *args, **kwargs):
            Command.CommandResult.__init__(self, *args, **kwargs)
            if self.result and 'path' in self.result:
                self.template_id = 'page/' + self.result['path'] + '/index'

    def command(self):
        return self._success(_('Rendered the page'), result={
            'path': (self.args and self.args[0] or ''),
            'data': self.data
        })


class ProgramStatus(Command):
    """Display list of running threads, locks and outstanding events."""
    SYNOPSIS = (None, 'ps', 'ps', None)
    ORDER = ('Internals', 5)
    CONFIG_REQUIRED = False
    IS_USER_ACTIVITY = False
    LOG_NOTHING = True

    class CommandResult(Command.CommandResult):
        def as_text(self):
            now = time.time()

            sessions = self.result.get('sessions')
            if sessions:
                sessions = '\n'.join(sorted(['  %s/%s = %s (%ds)'
                                             % (us['sessionid'],
                                                us['userdata'],
                                                us['userinfo'],
                                                now - us['timestamp'])
                                             for us in sessions]))
            else:
                sessions = '  ' + _('Nothing Found')

            ievents = self.result.get('ievents')
            cevents = self.result.get('cevents')
            if cevents:
                cevents = '\n'.join(['  %s %s' % (e.event_id, e.message)
                                     for e in cevents])
            else:
                cevents = '  ' + _('Nothing Found')

            ievents = self.result.get('ievents')
            if ievents:
                ievents = '\n'.join([' %s:%s %s' % (e.event_id,
                                                    e.flags,
                                                    e.message)
                                     for e in ievents])
            else:
                ievents = '  ' + _('Nothing Found')

            threads = self.result.get('threads')
            if threads:
                threads = '\n'.join(sorted([('  ' + str(t)) for t in threads]))
            else:
                threads = _('Nothing Found')

            locks = self.result.get('locks')
            if locks:
                locks = '\n'.join(sorted([('  %s.%s is %slocked'
                                           ) % (l[0], l[1],
                                                '' if l[2] else 'un')
                                          for l in locks]))
            else:
                locks = _('Nothing Found')

            return ('Recent events:\n%s\n\n'
                    'Events in progress:\n%s\n\n'
                    'Live sessions:\n%s\n\n'
                    'Postinglist timers:\n%s\n\n'
                    'Threads: (bg delay %.3fs, live=%s, httpd=%s)\n%s\n\n'
                    'Locks:\n%s'
                    ) % (cevents, ievents, sessions,
                         self.result['pl_timers'],
                         self.result['delay'],
                         self.result['live'],
                         self.result['httpd'],
                         threads, locks)

    def command(self, args=None):
        import mailpile.auth
        import mailpile.mail_source
        import mailpile.plugins.compose
        import mailpile.plugins.contacts

        config = self.session.config

        try:
            idx = config.index
            locks = [
                ('config.index', '_lock', idx._lock._is_owned()),
                ('config.index', '_save_lock', idx._save_lock._is_owned())
            ]
        except AttributeError:
            locks = []
        if config.vcards:
            locks.extend([
                ('config.vcards', '_lock', config.vcards._lock._is_owned()),
            ])
        locks.extend([
            ('config', '_lock', config._lock._is_owned()),
            ('mailpile.postinglist', 'GLOBAL_POSTING_LOCK',
             mailpile.postinglist.GLOBAL_POSTING_LOCK._is_owned()),
            ('mailpile.postinglist', 'GLOBAL_OPTIMIZE_LOCK',
             mailpile.plugins.compose.GLOBAL_EDITING_LOCK._is_owned()),
            ('mailpile.plugins.compose', 'GLOBAL_EDITING_LOCK',
             mailpile.plugins.contacts.GLOBAL_VCARD_LOCK._is_owned()),
            ('mailpile.plugins.contacts', 'GLOBAL_VCARD_LOCK',
             mailpile.postinglist.GLOBAL_OPTIMIZE_LOCK.locked()),
            ('mailpile.postinglist', 'GLOBAL_GPL_LOCK',
             mailpile.postinglist.GLOBAL_GPL_LOCK._is_owned()),
        ])

        threads = threading.enumerate()
        for thread in threads:
            try:
                if hasattr(thread, 'lock'):
                    locks.append([thread, 'lock', thread.lock])
                if hasattr(thread, '_lock'):
                    locks.append([thread, '_lock', thread._lock])
                if locks and hasattr(locks[-1][-1], 'locked'):
                    locks[-1][-1] = locks[-1][-1].locked()
                elif locks and hasattr(locks[-1][-1], '_is_owned'):
                    locks[-1][-1] = locks[-1][-1]._is_owned()
            except AttributeError:
                pass

        import mailpile.auth
        import mailpile.httpd
        result = {
            'sessions': [{'sessionid': k,
                          'timestamp': v.ts,
                          'userdata': v.data,
                          'userinfo': v.auth} for k, v in
                         mailpile.auth.SESSION_CACHE.iteritems()],
            'pl_timers': mailpile.postinglist.TIMERS,
            'delay': play_nice_with_threads(sleep=False),
            'live': mailpile.util.LIVE_USER_ACTIVITIES,
            'httpd': mailpile.httpd.LIVE_HTTP_REQUESTS,
            'threads': threads,
            'locks': sorted(locks)
        }
        if config.event_log:
            result.update({
                'cevents': list(config.event_log.events(flag='c'))[-10:],
                'ievents': config.event_log.incomplete(),
            })

        return self._success(_("Listed events, threads, and locks"),
                             result=result)


class ListDir(Command):
    """Display working directory listing"""
    SYNOPSIS = (None, 'ls', None, "<.../new/path/...>")
    ORDER = ('Internals', 5)
    CONFIG_REQUIRED = False
    IS_USER_ACTIVITY = True

    class CommandResult(Command.CommandResult):
        def as_text(self):
            if self.result:
                lines = []
                for fn, sz, isdir in self.result:
                    lines.append(('%10.10s  %s%s'
                                  ) % (sz, fn, isdir and '/' or ''))
                return '\n'.join(lines)
            else:
                return _('Nothing Found')

    def command(self, args=None):
        args = list((args is None) and self.args or args or [])

        if self.session.config.sys.lockdown:
            return self._error(_('In lockdown, doing nothing.'))

        try:
            file_list = [(f.decode('utf-8'),
                          os.path.getsize(f),
                          os.path.isdir(f))
                         for f in os.listdir('.') if not f.startswith('.')
                         and not args or [a for a in args if a in f]]
            file_list.sort(key=lambda i: i[0].lower())
            return self._success(_('Current directory is %s') % os.getcwd(),
                                 result=file_list)
        except (OSError, IOError, UnicodeDecodeError), e:
            return self._error(_('Failed to list directory: %s') % e)


class ChangeDir(ListDir):
    """Change working directory"""
    SYNOPSIS = (None, 'cd', None, "<.../new/path/...>")
    ORDER = ('Internals', 5)
    CONFIG_REQUIRED = False
    IS_USER_ACTIVITY = True

    def command(self, args=None):
        args = list((args is None) and self.args or args or [])

        if self.session.config.sys.lockdown:
            return self._error(_('In lockdown, doing nothing.'))

        try:
            os.chdir(args.pop(0).encode('utf-8'))
            return ListDir.command(self, args=args)
        except (OSError, IOError, UnicodeEncodeError), e:
            return self._error(_('Failed to change directories: %s') % e)


class CatFile(Command):
    """Dump the contents of a file, decrypting if necessary"""
    SYNOPSIS = (None, 'cat', None, "</path/to/file> [>/path/to/output]")
    ORDER = ('Internals', 5)
    CONFIG_REQUIRED = False
    IS_USER_ACTIVITY = True

    class CommandResult(Command.CommandResult):
        def as_text(self):
            if isinstance(self.result, list):
                return ''.join(self.result)
            else:
                return ''

    def command(self, args=None):
        lines = []
        files = list(args or self.args)

        if self.session.config.sys.lockdown:
            return self._error(_('In lockdown, doing nothing.'))

        target = tfd = None
        if files and files[-1] and files[-1][:1] == '>':
            target = files.pop(-1)[1:]
            if os.path.exists(target):
                return self._error(_('That file already exists: %s'
                                     ) % target)
            tfd = open(target, 'wb')
            cb = lambda ll: [tfd.write(l) for l in ll]
        else:
            cb = lambda ll: lines.extend((l.decode('utf-8') for l in ll))

        for fn in files:
            with open(fn, 'r') as fd:
                decrypt_and_parse_lines(fd, cb, self.session.config,
                                        newlines=True, decode=None)

        if tfd:
            tfd.close()
            return self._success(_('Dumped to %s: %s'
                                   ) % (target, ', '.join(files)))
        else:
            return self._success(_('Dumped: %s') % ', '.join(files),
                                   result=lines)


##[ Configuration commands ]###################################################

class ConfigSet(Command):
    """Change a setting"""
    SYNOPSIS = ('S', 'set', 'settings/set', '<section.variable> <value>')
    ORDER = ('Config', 1)
    CONFIG_REQUIRED = False
    IS_USER_ACTIVITY = True

    SPLIT_ARG = False

    HTTP_CALLABLE = ('POST', 'UPDATE')
    HTTP_STRICT_VARS = False
    HTTP_POST_VARS = {
        '_section': 'common section, create if needed',
        'section.variable': 'value|json-string'
    }

    def command(self):
        from mailpile.httpd import BLOCK_HTTPD_LOCK, Idle_HTTPD

        config = self.session.config
        args = list(self.args)
        ops = []

        if config.sys.lockdown:
            return self._error(_('In lockdown, doing nothing.'))

        if not config.loaded_config:
            self.session.ui.warning(_('WARNING: Any changes will '
                                      'be overwritten on login'))

        section = self.data.get('_section', [''])[0]
        if section:
            # Make sure section exists
            ops.append((section, '!CREATE_SECTION'))

        for var in self.data.keys():
            if var in ('_section', '_method'):
                continue
            sep = '/' if ('/' in (section+var)) else '.'
            svar = (section+sep+var) if section else var
            parts = svar.split(sep)
            if parts[0] in config.rules:
                if svar.endswith('[]'):
                    ops.append((svar[:-2], json.dumps(self.data[var])))
                else:
                    ops.append((svar, self.data[var][0]))
            else:
                raise ValueError(_('Invalid section or variable: %s') % var)

        if self.args:
            arg = ' '.join(self.args)
            if '=' in arg:
                # Backwards compatiblity with the old 'var = value' syntax.
                var, value = [s.strip() for s in arg.split('=', 1)]
                var = var.replace(': ', '.').replace(':', '.').replace(' ', '')
            else:
                var, value = arg.split(' ', 1)
            ops.append((var, value))

        # We don't have transactions really, but making sure the HTTPD
        # is idle (aside from this request) will definitely help.
        with BLOCK_HTTPD_LOCK, Idle_HTTPD():
            updated = {}
            for path, value in ops:
                value = value.strip()
                if value[:1] in ('{', '[') and value[-1:] in ( ']', '}'):
                    value = json.loads(value)
                try:
                    try:
                        cfg, var = config.walk(path.strip(), parent=1)
                        if value == '!CREATE_SECTION':
                            if var not in cfg:
                                cfg[var] = {}
                        else:
                            cfg[var] = value
                            updated[path] = value
                    except IndexError:
                        cfg, v1, v2 = config.walk(path.strip(), parent=2)
                        cfg[v1] = {v2: value}
                except TypeError:
                    raise ValueError('Could not set variable: %s' % path)

        if config.loaded_config:
            self._background_save(config=True)

        return self._success(_('Updated your settings'), result=updated)


class ConfigAdd(Command):
    """Add a new value to a list (or ordered dict) setting"""
    SYNOPSIS = (None, 'append', 'settings/add', '<section.variable> <value>')
    ORDER = ('Config', 1)
    SPLIT_ARG = False
    HTTP_CALLABLE = ('POST', 'UPDATE')
    HTTP_STRICT_VARS = False
    HTTP_POST_VARS = {
        'section.variable': 'value|json-string',
    }
    IS_USER_ACTIVITY = True

    def command(self):
        from mailpile.httpd import BLOCK_HTTPD_LOCK, Idle_HTTPD

        config = self.session.config
        ops = []

        if config.sys.lockdown:
            return self._error(_('In lockdown, doing nothing.'))

        for var in self.data.keys():
            parts = ('.' in var) and var.split('.') or var.split('/')
            if parts[0] in config.rules:
                ops.append((var, self.data[var][0]))

        if self.args:
            arg = ' '.join(self.args)
            if '=' in arg:
                # Backwards compatible with the old 'var = value' syntax.
                var, value = [s.strip() for s in arg.split('=', 1)]
                var = var.replace(': ', '.').replace(':', '.').replace(' ', '')
            else:
                var, value = arg.split(' ', 1)
            ops.append((var, value))

        # We don't have transactions really, but making sure the HTTPD
        # is idle (aside from this request) will definitely help.
        with BLOCK_HTTPD_LOCK, Idle_HTTPD():
            updated = {}
            for path, value in ops:
                value = value.strip()
                if value.startswith('{') or value.startswith('['):
                    value = json.loads(value)
                cfg, var = config.walk(path.strip(), parent=1)
                cfg[var].append(value)
                updated[path] = value

        if updated:
            self._background_save(config=True)

        return self._success(_('Updated your settings'), result=updated)


class ConfigUnset(Command):
    """Reset one or more settings to their defaults"""
    SYNOPSIS = ('U', 'unset', 'settings/unset', '<var>')
    ORDER = ('Config', 2)
    HTTP_CALLABLE = ('POST', )
    HTTP_POST_VARS = {
        'var': 'section.variables'
    }
    IS_USER_ACTIVITY = True

    def command(self):
        from mailpile.httpd import BLOCK_HTTPD_LOCK, Idle_HTTPD

        session, config = self.session, self.session.config

        if config.sys.lockdown:
            return self._error(_('In lockdown, doing nothing.'))

        def unset(cfg, key):
            if isinstance(cfg[key], dict):
                if '_any' in cfg[key].rules:
                    for skey in cfg[key].keys():
                        del cfg[key][skey]
                else:
                    for skey in cfg[key].keys():
                        unset(cfg[key], skey)
            elif isinstance(cfg[key], list):
                cfg[key] = []
            else:
                del cfg[key]

        # We don't have transactions really, but making sure the HTTPD
        # is idle (aside from this request) will definitely help.
        with BLOCK_HTTPD_LOCK, Idle_HTTPD():
            updated = []
            vlist = list(self.args) + (self.data.get('var', None) or [])
            for v in vlist:
                cfg, vn = config.walk(v, parent=True)
                unset(cfg, vn)

        if updated:
            self._background_save(config=True)

        return self._success(_('Reset to default values'), result=updated)


class ConfigPrint(Command):
    """Print one or more settings"""
    SYNOPSIS = ('P', 'print', 'settings', '[-short] <var>')
    ORDER = ('Config', 3)
    CONFIG_REQUIRED = False
    IS_USER_ACTIVITY = False

    HTTP_CALLABLE = ('GET', 'POST')
    HTTP_QUERY_VARS = {
        'var': 'section.variable',
        'short': 'Set True to omit unchanged values (defaults)'
    }
    HTTP_POST_VARS = {
        'user': 'Authenticate as user',
        'pass': 'Authenticate with password'
    }

    def _maybe_all(self, list_all, data, key_types):
        if isinstance(data, (dict, list)) and list_all:
            rv = {}
            for key in data.all_keys():
                if [t for t in data.key_types(key) if t not in key_types]:
                    # Silently omit things that are considered sensitive
                    continue
                rv[key] = data[key]
                if hasattr(rv[key], 'all_keys'):
                    rv[key] = self._maybe_all(True, rv[key], key_types)
            return rv
        return data

    def command(self):
        session, config = self.session, self.session.config
        result = {}
        invalid = []

        args = list(self.args)
        list_all = not self.data.get('short', ['-short' in args])[0]

        # FIXME: Shouldn't we suppress critical variables as well?
        key_types = ['public', 'critical']
        access_denied = False

        if self.data.get('_method') == 'POST':
            if 'pass' in self.data:
                from mailpile.auth import CheckPassword
                password = self.data['pass'][0]
                auth_user = CheckPassword(config,
                                          self.data.get('user', [None])[0],
                                          password)
                if auth_user == 'DEFAULT':
                    key_types += ['key']
                result['_auth_user'] = auth_user
                result['_auth_pass'] = password

        for key in (args + self.data.get('var', [])):
            if key in ('-short', ):
                continue
            try:
                data = config.walk(key, key_types=key_types)
                result[key] = self._maybe_all(list_all, data, key_types)
            except AccessError:
                access_denied = True
                invalid.append(key)
            except KeyError:
                invalid.append(key)

        if invalid:
            return self._error(_('Invalid keys'),
                               result=result, info={
                                   'keys': invalid,
                                   'key_types': key_types,
                                   'access_denied': access_denied
                               })
        else:
            return self._success(_('Displayed settings'), result=result)


class AddMailboxes(Command):
    """Add one or more mailboxes"""
    SYNOPSIS = ('A', 'add', None, '<path/to/mailbox>')
    ORDER = ('Config', 4)
    SPLIT_ARG = False
    HTTP_CALLABLE = ('POST', 'UPDATE')
    IS_USER_ACTIVITY = True

    MAX_PATHS = 50000

    def command(self):
        from mailpile.httpd import BLOCK_HTTPD_LOCK, Idle_HTTPD

        session, config = self.session, self.session.config
        adding = []
        existing = config.sys.mailbox
        paths = list(self.args)

        if config.sys.lockdown:
            return self._error(_('In lockdown, doing nothing.'))

        try:
            while paths:
                raw_fn = paths.pop(0)
                fn = os.path.normpath(os.path.expanduser(raw_fn))
                fn = os.path.abspath(fn)
                if raw_fn in existing or fn in existing:
                    session.ui.warning('Already in the pile: %s' % raw_fn)
                elif raw_fn.startswith("imap://"):
                    adding.append(raw_fn)
                elif IsMailbox(fn, config):
                    adding.append(raw_fn)
                elif os.path.exists(fn) and os.path.isdir(fn):
                        session.ui.mark('Scanning %s for mailboxes' % fn)
                        try:
                            for f in [f for f in os.listdir(fn)
                                      if not f.startswith('.')]:
                                paths.append(os.path.join(fn, f))
                                if len(paths) > self.MAX_PATHS:
                                    return self._error(_('Too many files'))
                        except OSError:
                            if raw_fn in self.args:
                                return self._error(_('Failed to read: %s'
                                                     ) % raw_fn)
                elif raw_fn in self.args:
                    return self._error(_('No such file or directory: %s'
                                         ) % raw_fn)
        except KeyboardInterrupt:
            return self._error(_('User aborted'))

        added = {}
        # We don't have transactions really, but making sure the HTTPD
        # is idle (aside from this request) will definitely help.
        with BLOCK_HTTPD_LOCK, Idle_HTTPD():
            for arg in adding:
                added[config.sys.mailbox.append(arg)] = arg
        if added:
            self._background_save(config=True)
            return self._success(_('Added %d mailboxes') % len(added),
                                 result={'added': added})
        else:
            return self._success(_('Nothing was added'))


###############################################################################

class Cached(Command):
    """Fetch results from the command cache."""
    SYNOPSIS = (None, 'cached', 'cached', '[<cache-id>]')
    ORDER = ('Internals', 7)
    HTTP_QUERY_VARS = {'id': 'Cache ID of command to redisplay'}
    IS_USER_ACTIVITY = False
    LOG_NOTHING = True

    def run(self):
        try:
            cid = self.args[0] if self.args else self.data.get('id', [None])[0]
            rv = self.session.config.command_cache.get_result(cid)
            self.session.copy(rv.session)
            return rv
        except:
            self._starting()
            self._ignore_exception()
            self._error(self.FAILURE % {'name': self.name,
                                        'args': ' '.join(self.args)})
            return self._finishing(self, False)


class Output(Command):
    """Choose format for command results."""
    SYNOPSIS = (None, 'output', None, '[json|text|html|<template>.html|...]')
    ORDER = ('Internals', 7)
    CONFIG_REQUIRED = False
    HTTP_STRICT_VARS = False
    HTTP_AUTH_REQUIRED = False
    IS_USER_ACTIVITY = False
    LOG_NOTHING = True

    def etag_data(self):
        return self.get_render_mode()

    def max_age(self):
        return 364 * 24 * 3600  # A long time!

    def get_render_mode(self):
        return self.args and self.args[0] or self.session.ui.render_mode

    def command(self):
        m = self.session.ui.render_mode = self.get_render_mode()
        return self._success(_('Set output mode to: %s') % m,
                             result={'output': m})


class Quit(Command):
    """Exit Mailpile, normal shutdown"""
    SYNOPSIS = ("q", "quit", "quitquitquit", None)
    ABOUT = ("Quit mailpile")
    ORDER = ("Internals", 2)
    CONFIG_REQUIRED = False
    RAISES = (KeyboardInterrupt,)

    def command(self):
        if self.session.config.sys.lockdown:
            return self._error(_('In lockdown, doing nothing.'))

        mailpile.util.QUITTING = True
        self._background_save(index=True, config=True, wait=True)
        try:
            import signal
            os.kill(mailpile.util.MAIN_PID, signal.SIGINT)
        except:
            def exiter():
                time.sleep(1)
                os._exit(0)
            threading.Thread(target=exiter).start()

        return self._success(_('Shutting down...'))


class TrustingQQQ(Command):
    """Allow anybody to quit the app"""
    SYNOPSIS = (None, "trustingqqq", None, None)

    def command(self):
        # FIXME: This is a hack to allow Windows deployments to shut
        #        down cleanly. Eventually this will take an argument
        #        specifying a random token that the launcher chooses.
        Quit.HTTP_AUTH_REQUIRED = False
        return self._success('OK, anybody can quit!')


class Abort(Command):
    """Force exit Mailpile (kills threads)"""
    SYNOPSIS = (None, "quit/abort", "abortabortabort", None)
    ABOUT = ("Quit mailpile")
    ORDER = ("Internals", 2)
    CONFIG_REQUIRED = False
    HTTP_QUERY_VARS = {
        'no_save': 'Do not try to save state'
    }

    def command(self):
        if self.session.config.sys.lockdown:
            return self._error(_('In lockdown, doing nothing.'))

        mailpile.util.QUITTING = True
        if 'no_save' not in self.data:
            self._background_save(index=True, config=True, wait=True,
                                  wait_callback=lambda: os._exit(1))
        else:
            os._exit(1)

        return self._success(_('Shutting down...'))


class Help(Command):
    """Print help on Mailpile or individual commands."""
    SYNOPSIS = ('h', 'help', 'help', '[<command-group>]')
    ABOUT = ('This is Mailpile!')
    ORDER = ('Config', 9)
    CONFIG_REQUIRED = False
    IS_USER_ACTIVITY = True

    class CommandResult(Command.CommandResult):

        def splash_as_text(self):
            text = [
                self.result['splash']
            ]

            if self.result['http_url']:
                text.append(_('The Web interface address is: %s'
                              ) % self.result['http_url'])
            else:
                text.append(_('The Web interface is disabled.'))

            text.append('')
            b = '   * '
            if self.result['interactive']:
                text.append(b + _('Type `help` for instructions or `quit` '
                                  'to quit.'))
                text.append(b + _('Long running operations can be aborted '
                                  'by pressing: <CTRL-C>'))
            if self.result['login_cmd'] and self.result['interactive']:
                text.append(b + _('You can log in using the `%s` command.'
                                  ) % self.result['login_cmd'])
            if self.result['in_browser']:
                text.append(b + _('Check your web browser!'))

            return '\n'.join(text)

        def variables_as_text(self):
            text = []
            for group in self.result['variables']:
                text.append(group['name'])
                for var in group['variables']:
                    sep = ('=' in var['type']) and ': ' or ' = '
                    text.append(('  %-35s %s'
                                 ) % (('%s%s<%s>'
                                       ) % (var['var'], sep,
                                            var['type'].replace('=', '> = <')),
                                      var['desc']))
                text.append('')
            return '\n'.join(text)

        def commands_as_text(self):
            text = [_('Commands:')]
            last_rank = None
            cmds = self.result['commands']
            width = self.result.get('width', 8)
            ckeys = cmds.keys()
            ckeys.sort(key=lambda k: (cmds[k][3], cmds[k][0]))
            arg_width = min(50, max(14, self.session.ui.term.max_width()-70))
            for c in ckeys:
                cmd, args, explanation, rank = cmds[c]
                if not rank or not cmd:
                    continue
                if last_rank and int(rank / 10) != last_rank:
                    text.append('')
                last_rank = int(rank / 10)
                if c[0] == '_':
                    c = '  '
                else:
                    c = '%s|' % c[0]
                fmt = '  %%s%%-%d.%ds' % (width, width)
                if explanation:
                    if len(args or '') <= arg_width:
                        fmt += ' %%-%d.%ds %%s' % (arg_width, arg_width)
                    else:
                        pad = len(c) + width + 3 + arg_width
                        fmt += ' %%s\n%s %%s' % (' ' * pad)
                else:
                    explanation = ''
                    fmt += ' %s %s '
                text.append(fmt % (c, cmd.replace('=', ''),
                                   args and ('%s' % (args, )) or '',
                                   (explanation.splitlines() or [''])[0]))
            if self.result.get('tags'):
                text.extend([
                    '',
                    _('Tags:  (use a tag as a command to display tagged '
                      'messages)'),
                    '',
                    self.result['tags'].as_text()
                ])
            return '\n'.join(text)

        def as_text(self):
            if not self.result:
                return _('Error')
            return ''.join([
                ('splash' in self.result) and self.splash_as_text() or '',
                (('variables' in self.result) and self.variables_as_text()
                 or ''),
                ('commands' in self.result) and self.commands_as_text() or '',
            ])

    def command(self):
        config = self.session.config
        self.session.ui.reset_marks(quiet=True)
        if self.args:
            command = self.args[0]
            for cls in COMMANDS:
                name = cls.SYNOPSIS[1] or cls.SYNOPSIS[2]
                width = len(name or '')
                if name and name == command:
                    order = 1
                    cmd_list = {'_main': (name, cls.SYNOPSIS[3],
                                          cls.__doc__, order)}
                    subs = [c for c in COMMANDS
                            if (c.SYNOPSIS[1] or c.SYNOPSIS[2] or ''
                                ).startswith(name + '/')]
                    for scls in sorted(subs):
                        sc, scmd, surl, ssynopsis = scls.SYNOPSIS[:4]
                        order += 1
                        cmd_list['_%s' % scmd] = (scmd, ssynopsis,
                                                  scls.__doc__, order)
                        width = max(len(scmd or surl), width)
                    return self._success(_('Displayed help'), result={
                        'pre': cls.__doc__,
                        'commands': cmd_list,
                        'width': width
                    })
            return self._error(_('Unknown command'))

        else:
            cmd_list = {}
            count = 0
            for grp in COMMAND_GROUPS:
                count += 10
                for cls in COMMANDS:
                    if cls.CONFIG_REQUIRED and not config.loaded_config:
                        continue
                    c, name, url, synopsis = cls.SYNOPSIS[:4]
                    if cls.ORDER[0] == grp and '/' not in (name or ''):
                        cmd_list[c or '_%s' % name] = (name, synopsis,
                                                       cls.__doc__,
                                                       count + cls.ORDER[1])
            if config.loaded_config:
                tags = GetCommand('tags')(self.session).run()
            else:
                tags = {}
            try:
                index = self._idx()
            except IOError:
                index = None
            return self._success(_('Displayed help'), result={
                'commands': cmd_list,
                'tags': tags,
                'index': index
            })

    def _starting(self):
        pass

    def _finishing(self, command, rv, *args, **kwargs):
        return self.CommandResult(self, self.session, self.name,
                                  command.__doc__ or self.__doc__, rv,
                                  self.status, self.message)


class HelpVars(Help):
    """Print help on Mailpile variables"""
    SYNOPSIS = (None, 'help/variables', 'help/variables', None)
    ABOUT = ('The available mailpile variables')
    ORDER = ('Config', 9)
    CONFIG_REQUIRED = False
    IS_USER_ACTIVITY = True

    def command(self):
        config = self.session.config.rules
        result = []
        categories = ["sys", "prefs", "profiles"]
        for cat in categories:
            variables = []
            what = config[cat]
            if isinstance(what[2], dict):
                for ii, i in what[2].iteritems():
                    variables.append({
                        'var': ii,
                        'type': str(i[1]),
                        'desc': i[0]
                    })
            variables.sort(key=lambda k: k['var'])
            result.append({
                'category': cat,
                'name': config[cat][0],
                'variables': variables
            })
        result.sort(key=lambda k: config[k['category']][0])
        return self._success(_('Displayed variables'),
                             result={'variables': result})


class HelpSplash(Help):
    """Print Mailpile splash screen"""
    SYNOPSIS = (None, 'help/splash', 'help/splash', None)
    ORDER = ('Config', 9)
    CONFIG_REQUIRED = False

    def command(self, interactive=True):
        from mailpile.auth import Authenticate
        http_worker = self.session.config.http_worker

        in_browser = False
        if http_worker:
            http_url = 'http://%s:%s/' % http_worker.httpd.sspec
            if ((sys.platform[:3] in ('dar', 'win') or os.getenv('DISPLAY'))
                    and self.session.config.prefs.open_in_browser):
                if BrowseOrLaunch.Browse(http_worker.httpd.sspec):
                    in_browser = True
                    time.sleep(2)
        else:
            http_url = ''

        return self._success(_('Displayed welcome message'), result={
            'splash': self.ABOUT,
            'http_url': http_url,
            'in_browser': in_browser,
            'login_cmd': (Authenticate.SYNOPSIS[1]
                          if not self.session.config.loaded_config else ''),
            'interactive': interactive
        })


def GetCommand(name):
    match = [c for c in COMMANDS if name in c.SYNOPSIS[:3]]
    if len(match) == 1:
        return match[0]
    return None


def Action(session, opt, arg, data=None):
    session.ui.reset_marks(quiet=True)
    config = session.config

    if not opt:
        return Help(session, 'help').run()

    # Use the COMMANDS dict by default.
    command = GetCommand(opt)
    if command:
        return command(session, opt, arg, data=data).run()

    # Tags are commands
    if config.loaded_config:
        tag = config.get_tag(opt)
        if tag:
            a = 'in:%s%s%s' % (tag.slug, ' ' if arg else'', arg)
            return GetCommand('search')(session, opt, arg=a, data=data).run()

    # OK, give up!
    raise UsageError(_('Unknown command: %s') % opt)


# Commands starting with _ don't get single-letter shortcodes...
COMMANDS = [
    Load, Optimize, Rescan, BrowseOrLaunch, RunWWW, ProgramStatus,
    ListDir, ChangeDir, CatFile,
    WritePID, ConfigPrint, ConfigSet, ConfigAdd, ConfigUnset, AddMailboxes,
    RenderPage, Cached, Output,
    Help, HelpVars, HelpSplash, Quit, TrustingQQQ, Abort
]
COMMAND_GROUPS = ['Internals', 'Config', 'Searching', 'Tagging', 'Composing']
