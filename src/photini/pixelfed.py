##  Photini - a simple photo metadata editor.
##  http://github.com/jim-easterbrook/Photini
##  Copyright (C) 2023  Jim Easterbrook  jim@jim-easterbrook.me.uk
##
##  This program is free software: you can redistribute it and/or
##  modify it under the terms of the GNU General Public License as
##  published by the Free Software Foundation, either version 3 of the
##  License, or (at your option) any later version.
##
##  This program is distributed in the hope that it will be useful,
##  but WITHOUT ANY WARRANTY; without even the implied warranty of
##  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
##  General Public License for more details.
##
##  You should have received a copy of the GNU General Public License
##  along with this program.  If not, see
##  <http://www.gnu.org/licenses/>.

import logging
import math
import os
from pprint import pprint
import re

import requests
from requests_oauthlib import OAuth2Session
from requests_toolbelt import MultipartEncoder, MultipartEncoderMonitor

from photini import __version__
from photini.configstore import BaseConfigStore, key_store
from photini.pyqt import (
    catch_all, execute, FormLayout, QtCore, QtSlot, QtWidgets, width_for_text)
from photini.uploader import (
    PhotiniUploader, UploadAborted, UploaderSession, UploaderUser)
from photini.widgets import DropDownSelector, MultiLineEdit, SingleLineEdit

logger = logging.getLogger(__name__)
translate = QtCore.QCoreApplication.translate

# Pixelfed API: https://docs.pixelfed.org/technical-documentation/api/
# Endpoint list: https://github.com/pixelfed/pixelfed/blob/dev/routes/api.php


class PixelfedSession(UploaderSession):
    name = 'pixelfed'

    def authorised(self):
        return self.api.authorized

    def open_connection(self):
        if self.api:
            return
        auto_refresh_kwargs = {
            'client_id': self.client_data['client_id'],
            'client_secret': self.client_data['client_secret'],
            }
        self.api = OAuth2Session(
            client_id=self.client_data['client_id'],
            token=self.user_data['token'],
            auto_refresh_url=self.client_data['api_base_url'] + '/oauth/token',
            auto_refresh_kwargs=auto_refresh_kwargs,
            token_updater=self.save_token)
        self.api.headers.update({'user-agent': 'Photini/' + __version__})
        self.media_ids = []

    def save_token(self, token):
        self.user_data['token'] = token
        self.new_token.emit(token)

    def api_call(self, endpoint, post=False, **params):
        self.open_connection()
        url = self.client_data['api_base_url'] + endpoint
        if post:
            return self.check_response(self.api.post(url, **params))
        return self.check_response(self.api.get(url, **params))

    @staticmethod
    def check_response(rsp):
        if rsp.status_code != 200:
            print(rsp.text)
            logger.error('HTTP error %d', rsp.status_code)
            return {}
        return rsp.json()

    def do_upload(self, fileobj, image_type, image, params):
        self.upload_progress.emit({'busy': False})
        fields = dict(params['media'])
        fields['file'] = (params['file_name'], fileobj, image_type)
        data = MultipartEncoderMonitor(
            MultipartEncoder(fields=fields), self.progress)
        rsp = self.api_call('/api/v1/media', post=True, data=data,
                            headers={'Content-Type': data.content_type})
        if not rsp:
            return ''
        self.upload_progress.emit({'busy': True})
        self.media_ids.append(rsp['id'])
        if not params['last_image']:
            return ''
        data = dict(params['status'])
        data['media_ids[]'] = self.media_ids
        self.media_ids = []
        rsp = self.api_call('/api/v1/statuses', post=True, data=data)
        return ''


class ChooseInstance(QtWidgets.QDialog):
    def __init__(self, default=None, instances=[], **kw):
        super(ChooseInstance, self).__init__(**kw)
        self.setWindowTitle(translate(
            'PixelfedTab', 'Photini: choose instance'))
        self.setLayout(QtWidgets.QVBoxLayout())
        # text
        self.layout().addWidget(QtWidgets.QLabel(
            '<h3>{}</h3>'.format(translate(
            'PixelfedTab', 'Choose an instance'))))
        self.layout().addWidget(QtWidgets.QLabel(translate(
            'PixelfedTab', 'Which Pixelfed instance hosts your account?')))
        # list of instances
        scroll_area = QtWidgets.QScrollArea()
        scroll_area.setWidgetResizable(True)
        self.layout().addWidget(scroll_area)
        panel = QtWidgets.QWidget()
        panel.setLayout(QtWidgets.QGridLayout())
        self.buttons = []
        row = 0
        for instance in instances:
            button = QtWidgets.QRadioButton(instance)
            button.setChecked(instance == default)
            self.buttons.append(button)
            panel.layout().addWidget(button, row, 0, 1, 2)
            row += 1
        # any other instance
        button = QtWidgets.QRadioButton(translate('PixelfedTab', 'Other'))
        self.buttons.append(button)
        button.setEnabled(False)
        panel.layout().addWidget(button, row, 0)
        self.other_text = QtWidgets.QLineEdit()
        self.other_text.textChanged.connect(self.text_changed)
        panel.layout().addWidget(self.other_text, row, 1)
        # add panel to scroll area now its size is known
        scroll_area.setWidget(panel)
        # ok & cancel buttons
        button_box = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        self.ok_button = button_box.addButton(
            QtWidgets.QDialogButtonBox.StandardButton.Ok)
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        self.layout().addWidget(button_box)
        for button in self.buttons:
            button.toggled.connect(self.button_toggled)
        self.button_toggled()

    @QtSlot(str)
    @catch_all
    def text_changed(self, value):
        self.buttons[-1].setChecked(bool(value))
        self.buttons[-1].setEnabled(bool(value))

    @QtSlot(bool)
    @catch_all
    def button_toggled(self, value=None):
        for button in self.buttons:
            if button.isChecked():
                self.ok_button.setEnabled(True)
                return
        self.ok_button.setEnabled(False)

    def execute(self):
        if execute(self) != QtWidgets.QDialog.DialogCode.Accepted:
            return None
        for button in self.buttons:
            if button.isChecked():
                if button == self.buttons[-1]:
                    return self.other_text.text().strip().strip('/')
                return button.text()
        return None


class PixelfedUser(UploaderUser):
    logger = logger
    name       = 'pixelfed'
    scopes     = ['read', 'write']

    def on_connect(self, widgets):
        with self.session(parent=self) as session:
            connected = session.authorised()
            yield 'connected', connected
            # get user info
            name, picture = None, None
            account = session.api_call('/api/v1/accounts/verify_credentials')
            self.user_data['id'] = account['id']
            name = account['display_name']
            # get icon
            icon_url = account['avatar_static']
            rsp = requests.get(icon_url)
            if rsp.status_code == 200:
                picture = rsp.content
            else:
                logger.error('HTTP error %d (%s)', rsp.status_code, icon_url)
            yield 'user', (name, picture)
            # get instance info
            self.version = {'mastodon': None, 'pixelfed': None}
            self.instance_config = session.api_call('/api/v1/instance')
            version = self.instance_config['version']
            match = re.match(r'(\d+)\.(\d+)\.(\d+)', version)
            if match:
                self.version['mastodon'] = tuple(
                    [int(x) for x in match.groups()])
            match = re.search(r'Pixelfed\s+(\d+)\.(\d+)\.(\d+)', version)
            if match:
                self.version['pixelfed'] = tuple(
                    [int(x) for x in match.groups()])
            self.unavailable['albums'] = not (
                self.version['pixelfed']
                and self.version['pixelfed'] >= (0, 11, 4))
            self.unavailable['comments_disabled'] = self.unavailable['albums']
            media = self.instance_config['configuration']['media_attachments']
            self.max_size = {
                'image': media['image_size_limit'],
                'image_pixels': media['image_matrix_limit'],
                'video': media['video_size_limit'],
                }
            # get user preferences
            prefs = session.api_call('/api/v1/preferences')
            widgets['sensitive'].setChecked(prefs['posting:default:sensitive'])
            widgets['visibility'].set_value(prefs['posting:default:visibility'])
            # get collections
            if self.unavailable['albums']:
                return
            for collection in session.api_call(
                    '/api/v1.1/collections/accounts/{}'.format(
                        self.user_data['id'])):
                yield 'album', {
                    'title': collection['title'],
                    'description': collection['description'],
                    'id': collection['id'],
                    }

    def load_user_data(self):
        self.config_store = QtWidgets.QApplication.instance().config_store
        # get list of known instances
        self.instances = []
        self.instance_data = {}
        # registered instances
        for section in key_store.config.sections():
            name, sep, instance = section.partition(' ')
            if name != 'pixelfed':
                continue
            instance_data = {'api_base_url': 'https://' + instance}
            for option in key_store.config.options(section):
                instance_data[option] = key_store.get(section, option)
            self.instances.append(instance)
            self.instance_data[instance] = instance_data
        # locally registered instances
        self.local_config = BaseConfigStore('pixelfed')
        for instance in self.local_config.config.sections():
            if instance in self.instances:
                continue
            instance_data = {'api_base_url': 'https://' + instance}
            for option in self.local_config.config.options(instance):
                instance_data[option] = self.local_config.get(instance, option)
            self.instances.append(instance)
            self.instance_data[instance] = instance_data
        # last used instance
        self.instance = self.config_store.get('pixelfed', 'instance')
        if not self.instance:
            return False
        self.client_data = self.instance_data[self.instance]
        # get user access token
        token = self.get_password()
        if not token:
            return False
        self.user_data['token'] = eval(token)
        return True

    def service_name(self):
        if 'token' in self.user_data:
            # logged in to a particular server
            return self.instance
        return translate('PixelfedTab', 'Pixelfed')

    def new_session(self, **kw):
        session = PixelfedSession(
            user_data=self.user_data, client_data=self.client_data, **kw)
        session.new_token.connect(self.new_token)
        return session

    def authorise(self):
        dialog = ChooseInstance(
            parent=self.parentWidget(), default=self.instance,
            instances=self.instances)
        instance = dialog.execute()
        if not instance:
            return
        if not self.register_app(instance):
            return
        self.instance = instance
        self.config_store.set('pixelfed', 'instance', instance)
        super(PixelfedUser, self).authorise()

    def register_app(self, instance):
        if instance in self.instance_data:
            self.client_data = self.instance_data[instance]
            return True
        # create new registration
        api_base_url = 'https://' + instance
        data = {
            'client_name': 'Photini',
            'scopes': ' '.join(self.scopes),
            'redirect_uris': 'http://127.0.0.1',
            'website': 'https://photini.readthedocs.io/',
            }
        rsp = PixelfedSession.check_response(requests.post(
            api_base_url + '/api/v1/apps', data=data, timeout=20))
        if not rsp:
            return False
        client_id = rsp['client_id']
        client_secret = rsp['client_secret']
        # store result
        self.local_config.set(instance, 'client_id', client_id)
        self.local_config.set(instance, 'client_secret', client_secret)
        self.local_config.save()
        self.client_data = {
            'api_base_url': api_base_url,
            'client_id': client_id,
            'client_secret': client_secret,
            }
        self.instances.append(instance)
        self.instance_data[instance] = self.client_data
        return True

    def auth_exchange(self, redirect_uri):
        with OAuth2Session(self.client_data['client_id'],
                           redirect_uri=redirect_uri,
                           scope=self.scopes) as session:
            authorization_url, state = session.authorization_url(
                self.client_data['api_base_url'] + '/oauth/authorize')
            result = yield authorization_url
            # often get more scopes than asked for, which upsets
            # requests_oauthlib
            os.environ['OAUTHLIB_RELAX_TOKEN_SCOPE'] = '1'
            token = session.fetch_token(
                self.client_data['api_base_url'] + '/oauth/token',
                client_secret=self.client_data['client_secret'],
                code=result['code'][0])
        if 'access_token' not in token:
            logger.info('No access token received')
            return
        self.new_token(token)
        self.connection_changed.emit(True)

    @QtSlot(dict)
    @catch_all
    def new_token(self, token):
        self.user_data['token'] = token
        self.set_password(repr(token))

class TabWidget(PhotiniUploader):
    logger = logger

    def __init__(self, *arg, **kw):
        self.user_widget = PixelfedUser()
        super(TabWidget, self).__init__(*arg, **kw)

    @staticmethod
    def tab_name():
        return translate('PixelfedTab', '&Pixelfed upload')

    def config_columns(self):
        ## first column
        column = QtWidgets.QGridLayout()
        column.setContentsMargins(0, 0, 0, 0)
        group = QtWidgets.QGroupBox()
        group.setLayout(FormLayout(wrapped=True))
        self.widget['status'] = MultiLineEdit('status')
        policy = self.widget['status'].sizePolicy()
        policy.setVerticalStretch(1)
        self.widget['status'].setSizePolicy(policy)
        group.layout().addRow(translate('PixelfedTab', 'Caption'),
                              self.widget['status'])
        column.addWidget(group, 0, 0)
        yield column, 1
        ## second column
        column = QtWidgets.QGridLayout()
        column.setContentsMargins(0, 0, 0, 0)
        group = QtWidgets.QGroupBox()
        group.setMinimumWidth(width_for_text(group, 'x' * 23))
        group.setLayout(FormLayout(wrapped=True))
        # visibility
        self.widget['visibility'] = DropDownSelector(
            'visibility', values = (
                (translate('PixelfedTab', 'Public'), 'public'),
                (translate('PixelfedTab', 'Followers only'), 'private'),
                (translate('PixelfedTab', 'Unlisted'), 'unlisted')),
            default='public', with_multiple=False)
        group.layout().addRow(translate('PixelfedTab', 'Post visibility'),
                              self.widget['visibility'])
        # sensitive
        self.widget['sensitive'] = QtWidgets.QCheckBox(
            translate('PixelfedTab', 'Sensitive post'))
        group.layout().addRow(self.widget['sensitive'])
        # comments disabled
        self.widget['comments_disabled'] = QtWidgets.QCheckBox(
            translate('PixelfedTab', 'Disable comments'))
        group.layout().addRow(self.widget['comments_disabled'])
        column.addWidget(group, 0, 0)
        yield column, 0
        ## last column is list of albums
        yield self.album_list(), 0

    def add_album(self, album, index=-1):
        if not album['title']:
            return None
        widget = QtWidgets.QCheckBox(album['title'].replace('&', '&&'))
        if album['description']:
            widget.setToolTip('<p>' + album['description'] + '</p>')
        widget.setProperty('id', album['id'])
        if index >= 0:
            self.widget['albums'].layout().insertWidget(index, widget)
        else:
            self.widget['albums'].layout().addWidget(widget)
        return widget

    def accepted_image_type(self, file_type):
        return file_type in self.user_widget.instance_config[
            'configuration']['media_attachments']['supported_mime_types']

    def get_conversion_function(self, image, params):
        if image.file_type.split('/')[0] == 'video':
            return 'omit'
        return self.prepare_image

    def ask_resize_image(self, image, resizable=False):
        if image.file_type.split('/')[0] == 'video':
            return super(TabWidget, self).ask_resize_image(
                image, resizable=resizable)
        return self.prepare_image

    def prepare_image(self, image):
        image = self.read_image(image)
        image = self.data_to_image(image)
        # reduce image size
        w, h = image['width'], image['height']
        shrink = math.sqrt(float(w * h) / float(self.user_widget.max_size['image_pixels']))
        if shrink > 1.0:
            w = int(float(w) / shrink)
            h = int(float(h) / shrink)
            image = self.resize_image(image, w, h)
        # convert mime type
        mime_type = image['mime_type']
        if mime_type not in self.user_widget.instance_config[
                'configuration']['media_attachments']['supported_mime_types']:
            mime_type = 'image/jpeg'
        image = self.image_to_data(
            image, mime_type=mime_type, max_size=self.user_widget.max_size['image'])
        return image['data'], image['mime_type']

    def get_upload_params(self, image):
        params = {'media': {}, 'status': {}}
        params['file_name'] = os.path.basename(image.path)
        # 'description' is the ALT text for an image
        description = []
        if image.metadata.alt_text:
            description.append(image.metadata.alt_text.default_text())
        if image.metadata.alt_text_ext:
            description.append(image.metadata.alt_text_ext.default_text())
        if description:
            params['media']['description'] = '\n\n'.join(description)
        # 'status' is the text that accompanies the media
        params['status']['status'] = self.widget[
            'status'].get_value() or params['file_name']
        params['status']['visibility'] = self.widget['visibility'].get_value()
        params['status']['sensitive'] = self.widget['sensitive'].isChecked()
        if (self.user_widget.version['pixelfed']
                and self.user_widget.version['pixelfed'] >= (0, 11, 4)):
            params['status']['comments_disabled'] = ('0', '1')[
                self.widget['comments_disabled'].isChecked()]
            # collections to add post to
            album_ids = []
            for child in self.widget['albums'].children():
                if child.isWidgetType() and child.isChecked():
                    album_ids.append(child.property('id'))
            if album_ids:
                params['status']['collection_ids[]'] = album_ids
        return params

    def enable_upload_button(self, selection=None):
        selection = selection or self.app.image_list.get_selected_images()
        super(TabWidget, self).enable_upload_button(selection=selection)
        if (self.buttons['upload'].isEnabled()
                and len(selection) > self.user_widget.instance_config[
                    'configuration']['statuses']['max_media_attachments']
                and not self.buttons['upload'].is_checked()):
            self.buttons['upload'].setEnabled(False)
            return
        result = {
            'title': [], 'headline': [], 'description': [], 'keywords': []}
        for image in selection:
            md = image.metadata
            if md.title:
                result['title'].append(md.title.default_text())
            if md.headline:
                result['headline'].append(str(md.headline))
            if md.description:
                result['description'].append(md.description.default_text())
            if md.keywords:
                result['keywords'] += md.keywords.human_tags()
        strings = []
        for key in ('title', 'headline', 'description'):
            if not result[key]:
                continue
            result[key].sort(key=lambda x: -len(x))
            string = result[key][0]
            for text in result[key][1:]:
                if text not in string:
                    string += '\n' + text
            strings.append(string)
        keywords = []
        for text in result['keywords']:
            if text not in keywords:
                keywords.append(text)
        if keywords:
            strings.append(' '.join(['#' + x for x in keywords]))
        self.widget['status'].set_value('\n\n'.join(strings))
