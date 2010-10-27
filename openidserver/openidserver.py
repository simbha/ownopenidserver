#!/usr/bin/env python

import os, os.path
import urlparse
import urllib
import sys
import hashlib, random

import web, web.http, web.form, web.session, web.contrib.template

import openid.server.server, openid.store.filestore
try:
    from openid.extensions import sreg
except ImportError:
    from openid import sreg


def read_hcard(url):
    # from ~isagalaev/scipio/trunk : /utils/__init__.py (revision 38)
    # Ivan Sagalaev, maniac@softwaremaniacs.org, 2010-05-05 19:12:52

    import re
    from urllib2 import urlopen
    import cgi
    from html5lib import HTMLParser

    try:
        f = urlopen(url)
        content_type = f.info().getheader('content-type', 'text/html')
        value, params = cgi.parse_header(content_type)
        charset = params.get('charset', 'utf-8').replace("'", '')
        dom = HTMLParser().parse(urlopen(url).read(512 * 1024).decode(charset, 'ignore'))
    except IOError:
        return

    def _find(node, class_name):
        for child in (c for c in node if c.type == 5):
            if re.search(r'\b%s\b' % class_name, child.attributes.get('class', '')):
                return child

    vcard = _find(dom, 'vcard')
    if vcard is None:
        return

    def _parse_property(class_name):
        el = _find(vcard, class_name)
        if el is None:
            return
        if el.name == 'abbr' and 'title' in el.attributes:
            result = el.attributes['title']
        else:
            result = u''.join(s.value for s in el if s.type == 4)
        return result.replace(u'\n', u' ').strip()

    def _get_gender():
        TITLES = {
                'mr': 'M',
                'ms': 'F',
                'mrs': 'F',
            }
        return TITLES.get(_parse_property('honorific-prefix'), None)

    def _get_bday():
        bday = _parse_property('bday')
        if bday:
            return bday[:10]
        else:
            return None

    return {
            'nickname': _parse_property('nickname') or _parse_property('fn'),
            'email': _parse_property('email'),
            'fullname': _parse_property('fn'),
            'dob': _get_bday(),
            'gender': _parse_property('x-gender') or _parse_property('gender') or _get_gender(),
            'postcode': _parse_property('postal-code'),
            'country': _parse_property('country-name'),
            'timezone': _parse_property('tz'),
        }


class TrustRootStore(object):
    """
    Store and lookup over trust root list
    """


    def __init__(self, directory):
        self.directory = directory
        if not os.path.exists(self.directory):
            os.makedirs(self.directory)


    def _get_filename(self, url):
        """
        Encode url to filename
        TODO: doctest
        """

        url = urlparse.urlparse(url)
        filename = urllib.quote('__'.join(tuple(url)).replace('/', '_'))

        return os.path.join(self.directory, filename)


    def items(self):
        return [(item, os.readlink(os.path.join(self.directory, item)))
                for item in os.listdir(self.directory)
                if os.path.islink(os.path.join(self.directory, item))]


    def add(self, url):
        return os.symlink(url, self._get_filename(url))


    def check(self, url):
        return os.path.lexists(self._get_filename(url))


    def delete(self, url):
        return os.unlink(self._get_filename(url))


class OpenIDResponse(object):
    """
    Handle requests to OpenID, including trust root lookups
    """


    class NoneRequest(Exception):
        """
        Raise if request is empty
        """
        pass


    class DecisionNeed(Exception):
        """
        Raise if user decision of approve or decline autorization need
        """
        pass


    class LogInNeed(Exception):
        """
        Raise if need user to be logged in
        """
        pass


    def _encode_response(self, response):
        self.response = response
        self.webresponse = self.openid.encodeResponse(self.response)
        return self.webresponse


    def __init__(self, server, openid, query):
        """
        Decode request
        """

        self.server = server
        self.openid = openid
        self.query = query

        # parse openid request
        self.request = self.openid.decodeRequest(query)


    def process(self, logged_in=False):
        """
        Main checks routine
        """

        # no request
        if self.request is None:
            raise OpenIDResponse.NoneRequest

        if self.request.mode in ["checkid_immediate", "checkid_setup"]:
            # check request

            if not logged_in:
                # this stage required restricted access to endpoint
                raise OpenIDResponse.LogInNeed

            if self.server.trust_root_store.check(self.request.trust_root):
                # approve if request from trustroot
                return self.approve()

            elif self.request.immediate:
                # decline if immediate and not in trustroot
                return self.decline()

            # last hope route to user decision
            raise OpenIDResponse.DecisionNeed


        # return openid.server.server.WebResponse
        return self._encode_response(self.openid.handleRequest(self.request))


    def approve(self, identity=None):
        """
        Approve request

        """

        if identity is None:
            identity = self.request.identity

        response = self.request.answer(
                allow=True,
                identity=identity
            )

        try:
            sreg_data = read_hcard(identity)
            if sreg_data:
                sreg_request = sreg.SRegRequest.fromOpenIDRequest(self.request)
                sreg_response = sreg.SRegResponse.extractResponse(sreg_request, sreg_data)
                response.addExtension(sreg_response)
        except:
            pass
            #TODO: fixme

        return self._encode_response(response)


    def always(self, identity=None):
        """
        Approve request and to append to trust root store
        """
        self.server.trust_root_store.add(self.request.trust_root)
        return self.approve(identity)


    def decline(self):
        """
        Decline request

        """
        return self._encode_response(self.request.answer(allow=False))


class OpenIDServer(object):
    """
    Manage OpenID server and trust root store, emit response
    """

    def __init__(self, openid_store, trust_root_store):
        self.openid_store = openid_store
        self.trust_root_store = trust_root_store


    def request(self, endpoint, query):
        openid_server = openid.server.server.Server(self.openid_store, endpoint)
        return OpenIDResponse(self, openid_server, query)


class PasswordManager(web.form.Validator):
    """
    Manage access password
    """

    _hashfunc = hashlib.sha512

    class NoPassword(Exception):
        pass

    def __init__(self, directory):
        self.directory = directory
        if not os.path.exists(self.directory):
            os.makedirs(self.directory)

        self.msg = u'Invalid password'


    def _get_filename(self):
        return os.path.join(self.directory, 'password')


    def _generate_hash(self, salt, password):
        """
        build hash as _hashfunc of concat of salt and password
        """
        hash = self._hashfunc()
        hash.update(salt.encode('utf8'))
        hash.update(password.encode('utf8'))
        return hash.hexdigest()


    def valid(self, password):
        """
        Check password. Return False if passwords don't match, else return True if
        passwords match or unavailable
        """
        try:
            file = open(self._get_filename(), 'rb+')

            # read salt and hash splitted by '$' from password file
            salt, hash = file.read().strip().split('$', 1)

            file.close()

            # build hash and compare with stored
            if not self._generate_hash(salt, password) == hash:
                return False

        except:
            raise PasswordManager.NoPassword

        return True


    def set(self, password):
        """
        Set password
        """
        try:
            file = open(self._get_filename(), 'wb+')
            salt = str(random.randint(1, sys.maxint))

            file.write('$'.join([salt, self._generate_hash(salt, password)]))

            file.close()
            return True
        except:
            raise


def render_openid_to_response(response):
    """
    Return WebResponse as web.py response
    """
    if response.code in [200]:
        for name, value in response.headers.items():
            web.header(name, value)
        return response.body
    elif response.code in [302] and response.headers.has_key('location'):
        return web.found(response.headers['location'])
    else:
        return web.HTTPError(str(response.code) + ' ', response.headers)


class WebHandler(object):


    def __init__(self):
        self.query = web.input()
        self.method = None


    def GET(self, *args, **kwargs):
        self.method = 'GET'
        return self.request(*args, **kwargs)


    def POST(self, *args, **kwargs):
        self.method = 'POST'
        return self.request(*args, **kwargs)


    def request(self):
        raise NotImplemented


class WebOpenIDIndex(WebHandler):


    def request(self):
        web.header('Content-type', 'text/html')
        return render.base(
                logged_in=session.get('logged_in', False),
                login_url=web.ctx.homedomain + web.url('/account/login'),
                logout_url=web.ctx.homedomain + web.url('/account/logout'),
                change_password_url=web.ctx.homedomain + web.url('/account/change_password'),
                check_trusted_url=web.ctx.homedomain + web.url('/account/trusted'),
                no_password=session.get('no_password', False),
                endpoint=web.ctx.homedomain + web.url('/endpoint'),
                yadis=web.ctx.homedomain + web.url('/yadis.xrds'),
            )


def WebOpenIDLoginRequired(query):
    query['return_to'] = web.ctx.homedomain + web.url(web.ctx.path)
    return web.found(web.ctx.homedomain + web.url('/account/login', **query))


def WebOpenIDLoginForm(validator):
    return web.form.Form(
            web.form.Password("password",
                validator,
                description="Password: ",
            ),
        )


class WebOpenIDLogin(WebHandler):


    def request(self):
        return_to = self.query.get('return_to', web.ctx.homedomain + web.url('/account'))

        data = filter(lambda item: item[0] not in ['password'], self.query.items())

        form = WebOpenIDLoginForm(password_manager)()

        session['no_password'] = False

        if self.method == 'POST':
            try:
                if form.validates(self.query):
                    session['logged_in'] = True
                    return web.found(return_to + '?' + web.http.urlencode(dict(data)))

            except PasswordManager.NoPassword:
                session['no_password'] = True
                session['logged_in'] = True
                return web.found(return_to + '?' + web.http.urlencode(dict(data)))

        web.header('Content-type', 'text/html')
        return render.login(
                logged_in=session.get('logged_in', False),
                login_url=web.ctx.homedomain + web.url('/account/login'),
                logout_url=web.ctx.homedomain + web.url('/account/logout'),
                change_password_url=web.ctx.homedomain + web.url('/account/change_password'),
                no_password=session.get('no_password', False),
                form=form,
                query=data,
            )


class WebOpenIDLogout(WebHandler):


    def request(self):
        session['logged_in'] = False
        return web.found(web.ctx.homedomain + web.url('/account/login'))


WebOpenIDChangePasswordForm = web.form.Form(
            web.form.Password("password",
                web.form.notnull,
                description="Password: ",
            ),
            web.form.Password("confirm",
                web.form.notnull,
                description="Retype: ",
            ),
            validators=[
                    web.form.Validator('Passwords did not match',
                        lambda source: source['password'] == source['confirm']),
                ],
        )


class WebOpenIDChangePassword(WebHandler):


    def request(self):
        # check for login
        logged_in = session.get('logged_in', False)

        if not logged_in:
            return WebOpenIDLoginRequired(self.query)

        form = WebOpenIDChangePasswordForm()

        if self.method == 'POST':
            if form.validates(self.query):
                password_manager.set(self.query['password'])

                session['no_password'] = False

                return web.found(web.ctx.homedomain + web.url('/account'))

        web.header('Content-type', 'text/html')
        return render.password(
                logged_in=session.get('logged_in', False),
                logout_url=web.ctx.homedomain + web.url('/account/logout'),
                change_password_url=web.ctx.homedomain + web.url('/account/change_password'),
                no_password=session.get('no_password', False),
                form=form,
            )


class WebOpenIDTrusted(WebHandler):


    def request(self):
        # check for login
        logged_in = session.get('logged_in', False)

        if not logged_in:
            return WebOpenIDLoginRequired(self.query)

        items = [
                ((
                    item[1],
                    web.ctx.homedomain + web.url('/account/trusted/%s/delete' % item[0])
                ))
                for item in trust_root_store.items()
            ]

        removed = session.get('trusted_removed_successful', False)
        session['trusted_removed_successful'] = False

        web.header('Content-type', 'text/html')
        return render.trusted(
                logged_in=session.get('logged_in', False),
                logout_url=web.ctx.homedomain + web.url('/account/logout'),
                change_password_url=web.ctx.homedomain + web.url('/account/change_password'),
                no_password=session.get('no_password', False),
                trusted=items,
                removed=removed,
            )


class WebOpenIDTrustedDelete(WebHandler):


    def request(self, trusted_id):
        # check for login
        logged_in = session.get('logged_in', False)

        if not logged_in:
            return WebOpenIDLoginRequired(self.query)

        try:
            trust_root = dict(trust_root_store.items())[trusted_id]
        except:
            return web.notfound()

        if self.method == 'POST':
                trust_root_store.delete(trust_root)

                session['trusted_removed_successful']  = True

                return web.found(web.ctx.homedomain + web.url('/account/trusted'))

        web.header('Content-type', 'text/html')
        return render.trusted_confirm(
                logged_in=session.get('logged_in', False),
                logout_url=web.ctx.homedomain + web.url('/account/logout'),
                change_password_url=web.ctx.homedomain + web.url('/account/change_password'),
                check_trusted_url=web.ctx.homedomain + web.url('/account/trusted'),
                trusted_remove_url=web.ctx.homedomain + web.url('/account/trusted/%s/delete' % trusted_id),
                no_password=session.get('no_password', False),
                trust_root=trust_root,
            )


class WebOpenIDYadis(WebHandler):


    def request(self):
        import openid.consumer
        web.header('Content-type', 'application/xrds+xml')
        return """<?xml version="1.0" encoding="UTF-8"?>\n<xrds:XRDS \
                xmlns:xrds="xri://$xrds" xmlns="xri://$xrd*($v*2.0)"><XRD><Service \
                priority="0"><Type>%s</Type><Type>%s</Type><URI>%s</URI><LocalID>%s</LocalID></Service></XRD></xrds:XRDS>""" %\
            (
                openid.consumer.discover.OPENID_2_0_TYPE,
                openid.consumer.discover.OPENID_1_0_TYPE,
                web.ctx.homedomain + web.url('/endpoint'),
                web.ctx.homedomain,
            )


class WebOpenIDEndpoint(WebHandler):


    def request(self):
        # check for login
        logged_in = session.get('logged_in', False)

        request = server.request(web.ctx.homedomain + web.url('/endpoint'), self.query)
        try:
            response = request.process(logged_in)

        except OpenIDResponse.NoneRequest:
            return web.badrequest()

        except OpenIDResponse.LogInNeed:
            # redirect request to login form
            return WebOpenIDLoginRequired(self.query)

        except OpenIDResponse.DecisionNeed:
            # redirect request to decision page in restricted area
            return web.found(web.ctx.homedomain + web.url('/account/decision', **self.query))

        return render_openid_to_response(response)


class WebOpenIDDecision(WebHandler):


    def request(self):
        # check for login
        logged_in = session.get('logged_in', False)

        if not logged_in:
            return WebOpenIDLoginRequired(self.query)

        request = server.request(web.ctx.homedomain + web.url('/endpoint'), self.query)

        try:
            response = request.process(logged_in=True)

        except OpenIDResponse.NoneRequest:
            return web.badrequest()

        except OpenIDResponse.DecisionNeed:
            if self.method == 'POST':
                if self.query.has_key('approve'):
                    response = request.approve()
                elif self.query.has_key('always'):
                    response = request.always()
                else:
                    response = request.decline()
            else:
                data = filter(lambda item: item[0] not in ['approve', 'always'], self.query.items())

                sreg_request = sreg.SRegRequest.fromOpenIDRequest(request.request)

                if sreg_request.required or sreg_request.optional:
                    try:
                        hcard = read_hcard(request.request.identity) or dict()
                    except:
                        hcard = dict()

                    TRANSLATION = {
                            'fullname': { '__name__': 'Full name' },
                            'dob': { '__name__': 'Date of Birth' },
                            'gender': { 'M': 'Male', 'F': 'Female' },
                            'postcode': { '__name__': 'Postal code' },
                        }

                    required = [
                            (
                                TRANSLATION.get(field, {}).get('__name__', field.title()),
                                TRANSLATION.get(field, {}).get(hcard.get(field, None), hcard.get(field, None)),
                            )
                            for field in (sreg_request.required + sreg_request.optional)
                        ]
                else:
                    required = None

                web.header('Content-type', 'text/html')
                return render.verify(
                        logged_in=logged_in,
                        logout_url=web.ctx.homedomain + web.url('/account/logout'),
                        change_password_url=web.ctx.homedomain + web.url('/account/change_password'),
                        no_password=session.get('no_password', False),
                        decision_url=web.ctx.homedomain + web.url('/account/decision'),
                        identity=request.request.identity,
                        trust_root=request.request.trust_root,
                        required=required,
                        query=data,
                    )

        return render_openid_to_response(response)


def init(
            root_store_path,
            trust_root_store_path,
            session_store_path,
            password_store_path,
            templates_path,
            debug=False
        ):

    context = globals()

    app = web.application(
            (
                '', 'WebOpenIDIndex',
                '/', 'WebOpenIDIndex',
                '/account', 'WebOpenIDIndex',
                '/account/login', 'WebOpenIDLogin',
                '/account/logout', 'WebOpenIDLogout',
                '/account/change_password', 'WebOpenIDChangePassword',
                '/account/trusted', 'WebOpenIDTrusted',
                '/account/trusted/(?P<trusted_id>[^/]+)/delete', 'WebOpenIDTrustedDelete',
                '/yadis.xrds', 'WebOpenIDYadis',
                '/endpoint', 'WebOpenIDEndpoint',
                '/account/decision', 'WebOpenIDDecision',
            ),
            context,
        )


    openid_store = openid.store.filestore.FileOpenIDStore(root_store_path)
    trust_root_store = TrustRootStore(trust_root_store_path)
    server = OpenIDServer(openid_store, trust_root_store)
    context['trust_root_store'] = trust_root_store
    context['server'] = server

    sessions_store = web.session.DiskStore(session_store_path)
    session = web.session.Session(app, sessions_store)
    context['session'] = session

    password_manager = PasswordManager(password_store_path)
    context['password_manager'] = password_manager

    render = web.contrib.template.render_jinja(templates_path)
    context['render'] = render

    web.config.debug = debug

    return app


if __name__ == '__main__':
    ROOT_STORE = 'sstore'
    TEMPLATES = 'templates'

    TRUST_ROOT_STORE = os.path.join(ROOT_STORE, 'trust_root')
    SESSION_STORE = os.path.join(ROOT_STORE, 'sessions')
    PASSWORD_STORE = ROOT_STORE
    init(ROOT_STORE, TRUST_ROOT_STORE, SESSION_STORE, PASSWORD_STORE, TEMPLATES, True).run()