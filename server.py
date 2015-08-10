# coding=utf-8
#from gevent import monkey
#monkey.patch_all()

import flask
import datetime
from flask import Flask, redirect, session
from flask.json import JSONEncoder
from flask_restful import Resource, Api, abort, reqparse, request
from monitorrent.engine import Logger, EngineRunner
from monitorrent.db import init_db_engine, create_db, upgrade
from monitorrent.plugin_managers import load_plugins, get_all_plugins, upgrades, TrackersManager, ClientsManager
from flask_socketio import SocketIO, emit
from monitorrent.plugins.trackers import TrackerPluginWithCredentialsBase
from monitorrent.settings_manager import SettingsManager
from functools import wraps

init_db_engine("sqlite:///monitorrent.db", True)
load_plugins()
upgrade(get_all_plugins(), upgrades)

settings_manager = SettingsManager()

create_db()

tracker_manager = TrackersManager()
clients_manager = ClientsManager()


def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if settings_manager.get_is_authentication_enabled() and not session.get('user', False):
            return abort(401)
        return f(*args, **kwargs)
    return decorated


class SecuredStaticFlask(Flask):
    not_auth_files = ['favicon.ico', 'styles/monitorrent.css', 'login.html']

    def send_static_file(self, filename):
        if not settings_manager.get_is_authentication_enabled() or \
                (session.get('user', False) or filename in self.not_auth_files):
            return super(SecuredStaticFlask, self).send_static_file(filename)
        else:
            abort(401)


class MonitorrentJSONEncoder(JSONEncoder):
    def default(self, o):
        if isinstance(o, datetime.datetime):
            return o.isoformat()
        return super(MonitorrentJSONEncoder, self).default(o)

static_folder = "webapp"
app = SecuredStaticFlask(__name__, static_folder=static_folder, static_url_path='')
app.json_encoder = MonitorrentJSONEncoder

app.config['SECRET_KEY'] = 'secret!'
app.config['JSON_AS_ASCII'] = False
app.config['RESTFUL_JSON'] = {'ensure_ascii': False, 'cls': app.json_encoder}
socketio = SocketIO(app)


class EngineWebSocketLogger(Logger):
    def started(self):
        socketio.emit('started', namespace='/execute')

    def finished(self, finish_time, exception):
        args = {
            'finish_time': finish_time.isoformat(),
            'exception': exception.message if exception else None
        }
        socketio.emit('finished', args, namespace='/execute')

    def info(self, message):
        self.emit('info', message)

    def failed(self, message):
        self.emit('failed', message)

    def downloaded(self, message, torrent):
        self.emit('downloaded', message, size=len(torrent))

    def emit(self, level, message, **kwargs):
        data = {'level': level, 'message': message}
        data.update(kwargs)
        socketio.emit('log', data, namespace='/execute')


engine_runner = EngineRunner(EngineWebSocketLogger(), tracker_manager, clients_manager)

class Topics(Resource):
    url_parser = reqparse.RequestParser()

    def __init__(self):
        super(Topics, self).__init__()
        self.url_parser.add_argument('url', required=True)

    @requires_auth
    def get(self):
        return tracker_manager.get_watching_torrents()

    @requires_auth
    def post(self):
        json = request.get_json()
        url = json.get('url', None)
        settings = json.get('settings', None)
        added = tracker_manager.add_topic(url, settings)
        if not added:
            abort(400, message='Can\'t add torrent: \'{}\''.format(url))
        return None, 201


class Topic(Resource):
    @requires_auth
    def get(self, id):
        watch = tracker_manager.get_topic(id)
        return watch

    @requires_auth
    def put(self, id):
        settings = request.get_json()
        updated = tracker_manager.update_watch(id, settings)
        if not updated:
            abort(404, message='Can\'t update torrent {}'.format(id))
        return None, 204

    @requires_auth
    def delete(self, id):
        deleted = tracker_manager.remove_topic(id)
        if not deleted:
            abort(404, message='Torrent {} doesn\'t exist'.format(id))
        return None, 204

class Clients(Resource):
    @requires_auth
    def get(self, client):
        result = clients_manager.get_settings(client)
        if not result:
            abort(404, message='Client \'{}\' doesn\'t exist'.format(client))
        return result

    @requires_auth
    def put(self, client):
        settings = request.get_json()
        clients_manager.set_settings(client, settings)
        return None, 204


class ClientList(Resource):
    @requires_auth
    def get(self):
        return [{'name': n, 'form': c.form} for n, c in clients_manager.clients.iteritems()]


class Trackers(Resource):
    @requires_auth
    def get(self, tracker):
        result = tracker_manager.get_settings(tracker)
        if not result:
            abort(404, message='Client \'{}\' doesn\'t exist'.format(tracker))
        return result

    @requires_auth
    def put(self, tracker):
        settings = request.get_json()
        tracker_manager.set_settings(tracker, settings)
        return None, 204


class TrackerList(Resource):
    @requires_auth
    def get(self):
        return [{'name': name, 'form': tracker.credentials_form} for name, tracker in tracker_manager.trackers.items()
                if isinstance(tracker, TrackerPluginWithCredentialsBase)]


class Execute(Resource):
    @requires_auth
    def get(self):
        return {
            "interval": engine_runner.interval,
            "last_execute": engine_runner.last_execute
        }

    @requires_auth
    def put(self):
        settings = request.get_json()
        if 'interval' in settings:
            engine_runner.interval = int(settings['interval'])
            return None, 204
        return None, 400


class Login(Resource):
    def post(self):
        login_form_parser = reqparse.RequestParser()
        login_form_parser.add_argument('password', required=True)
        login_form = login_form_parser.parse_args()
        if login_form.password == settings_manager.get_password():
            session['user'] = True
            return {'status': 'Ok', 'result': 'Successfull'}
        return {'status': 'Unauthorized', 'result': 'Wrong password'}, 401


class PasswordSettings(Resource):
    def put(self):
        password_settings_parser = reqparse.RequestParser()
        password_settings_parser.add_argument('old_password', required=True)
        password_settings_parser.add_argument('new_password', required=True)
        password_settings = password_settings_parser.parse_args()
        if password_settings.old_password != settings_manager.get_password():
            return {'status': 'Bad Request', 'message': 'Old password is wrong', 'param': 'old_password'}, 400
        if not password_settings.new_password:
            return {'status': 'Bad Request', 'message': 'New password is required', 'param': 'new_password'}, 400
        settings_manager.set_password(password_settings.new_password)
        return None, 204


class AuthenticationSettings(Resource):
    def get(self):
        return {'is_authentication_enabled': settings_manager.get_is_authentication_enabled()}

    def put(self):
        settings_parser = reqparse.RequestParser()
        settings_parser.add_argument('password', required=True)
        settings_parser.add_argument('is_authentication_enabled', required=True, type=bool)
        settings = settings_parser.parse_args()
        if settings.password != settings_manager.get_password():
            return {'status': 'Bad Request', 'message': 'Wrong password', 'param': 'password'}, 400
        settings_manager.set_is_authentication_enabled(settings.is_authentication_enabled)
        return None, 204


class Logout(Resource):
    def post(self):
        if 'user' in session:
            del session['user']
        return None, 201


@socketio.on('execute', namespace='/execute')
def execute():
    engine_runner.execute()

@app.route('/')
def index():
    if not settings_manager.get_is_authentication_enabled() or session.get('user', False):
        return app.send_static_file('index.html')
    return redirect('/login')


@app.route('/login')
def login():
    if not settings_manager.get_is_authentication_enabled() or session.get('user', False):
        return redirect('/')
    return app.send_static_file('login.html')


@app.route('/api/parse')
@requires_auth
def parse_url():
    url = request.args['url']
    # parse_url is separate and internal method,
    # but for this request we need initial settings before add_topic
    title = tracker_manager.prepare_add_topic(url)
    if title:
        return flask.jsonify(**title)
    abort(400, message='Can\' parse url: \'{}\''.format(url))

@app.route('/api/check_client')
@requires_auth
def check_client():
    client = request.args['client']
    return '', 204 if clients_manager.check_connection(client) else 500

@app.route('/api/check_tracker')
@requires_auth
def check_tracker():
    client = request.args['tracker']
    return '', 204 if tracker_manager.check_connection(client) else 500


@socketio.on_error
def error_handler(e):
    print e


@socketio.on_error_default
def default_error_handler(e):
    print e

api = Api(app)
api.add_resource(Topic, '/api/topics/<int:id>')
api.add_resource(Topics, '/api/topics')
api.add_resource(ClientList, '/api/clients')
api.add_resource(Clients, '/api/clients/<string:client>')
api.add_resource(TrackerList, '/api/trackers')
api.add_resource(Trackers, '/api/trackers/<string:tracker>')
api.add_resource(Execute, '/api/execute')
api.add_resource(Login, '/api/login', endpoint='api_login')
api.add_resource(Logout, '/api/logout', endpoint='api_logout')
api.add_resource(AuthenticationSettings, '/api/settings/authentication')
api.add_resource(PasswordSettings, '/api/settings/password')

if __name__ == '__main__':
    #app.run(host='0.0.0.0', debug=True)
    socketio.run(app, host='0.0.0.0')
