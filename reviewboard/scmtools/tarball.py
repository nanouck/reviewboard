from __future__ import unicode_literals

import logging
import os
import re
import platform
import tarfile

from django.utils import six
from django.utils.six.moves.urllib.error import HTTPError
from django.utils.six.moves.urllib.parse import quote as urlquote
from django.utils.six.moves.urllib.request import (Request as URLRequest,
                                                   urlopen)
from django.utils.translation import ugettext_lazy as _
from djblets.util.filesystem import is_exe_in_path

from reviewboard.diffviewer.parser import DiffParser, DiffParserError, File
from reviewboard.scmtools.core import SCMClient, SCMTool, HEAD, PRE_CREATION
from reviewboard.scmtools.errors import (FileNotFoundError,
                                         InvalidRevisionFormatError,
                                         RepositoryNotFoundError,
                                         SCMError)
try:
    import urlparse
    uses_netloc = urlparse.uses_netloc
    urllib_urlparse = urlparse.urlparse
except ImportError:
    import urllib.parse
    uses_netloc = urllib.parse.uses_netloc
    urllib_urlparse = urllib.parse.urlparse


class TarballTool(SCMTool):
    name = "Tarball"
    field_help_text = {
        'path': _('Path of tarball to make request against. Could be a remote '
                  'path (HTTP url) or a local one.')
    }
    dependencies = {
        'modules': ['tarfile'],
    }

    def __init__(self, repository):
        super(TarballTool, self).__init__(repository)

        local_site_name = None

        if repository.local_site:
            local_site_name = repository.local_site.name

        credentials = repository.get_credentials()

        self.client = TarballClient(repository.path,
                                    credentials['username'],
                                    credentials['password'],
                                    repository.encoding, local_site_name)

    def get_file(self, path, revision):
        if revision == PRE_CREATION:
            return ""

        return self.client.get_file(path, revision)

    def file_exists(self, path, revision):
        if revision == PRE_CREATION:
            return False

        try:
            return self.client.get_file_exists(path, revision)
        except (FileNotFoundError, InvalidRevisionFormatError):
            return False

    def parse_diff_revision(self, file_str, revision_str, moved=False,
                            copied=False, *args, **kwargs):
        revision = revision_str

        if file_str == "/dev/null":
            revision = PRE_CREATION

        return file_str, revision

    def get_diffs_use_absolute_paths(self):
        return True

    def get_fields(self):
        return ['diff_path', 'parent_diff_path']

    def get_parser(self, data):
        return DiffParser(data)

    @classmethod
    def check_repository(cls, path, username=None, password=None,
                         local_site_name=None):
        """
        Performs checks on a repository to test its validity.

        This should check if a repository exists and can be connected to.
        This will also check if the repository requires an HTTPS certificate.

        The result is returned as an exception. The exception may contain
        extra information, such as a human-readable description of the problem.
        If the repository is valid and can be connected to, no exception
        will be thrown.
        """
        client = TarballClient(path, local_site_name=local_site_name)

        super(TarballTool, cls).check_repository(client.path, username, password,
                                                 local_site_name)

        if not client.is_valid_repository():
            raise RepositoryNotFoundError()

        # TODO: Check for an HTTPS certificate. This will require pycurl.


class TarballClient(SCMClient):
    TARBALL_LOCAL_CACHE_DIR = '/tmp'

    def __init__(self, path, username=None, password=None,
                 encoding='', local_site_name=None):
        super(TarballClient, self).__init__(self._normalize_tarball_url(path),
                                            username=username,
                                            password=password)

        if not is_exe_in_path('tar'):
            # This is technically not the right kind of error, but it's the
            # pattern we use with all the other tools.
            raise ImportError

        self.encoding = encoding
        self.local_site_name = local_site_name
        tarball_filename = os.path.basename(self.path)

        if self.path.startswith('file://'):
            self.tarball_local_path = self.path.replace('file://','')
        else:
            self.tarball_local_path = os.path.join(
                self.TARBALL_LOCAL_CACHE_DIR,
                tarball_filename)

    def is_valid_repository(self):
        """Checks if this is a valid Tarball repository."""
        if self.path.startswith('http://'):
            try:
                self._cache_remote_tarball()
            except Exception as e:
                logging.error("Tarball: Cannot cache remote tarball %s: %s" %
                              (self.path, str(e)))
                return False
        if not os.path.exists(self.tarball_local_path):
            logging.error("Tarball: Cannot find local archive %s" %
                              (self.tarball_local_path))
            return False
        return self._is_valid_tarfile()

    def get_file(self, path, revision):
        self._open_tarfile()
        try:
            tarinfo = self.tarfile.getmember(path)
        except KeyError:
            logging.error("Tarball: Failed to find %s from %s" %
                          (path, self.path))
            raise FileNotFoundError(path, revision)
        return tarfile.extractfile(tarinfo)

    def get_file_exists(self, path, revision):
        try:
            # We want to make sure we can access the file successfully,
            # without any HTTP errors. A successful access means the file
            # exists. The contents themselves are meaningless, so ignore
            # them. If we do successfully get the file without triggering
            # any sort of exception, then the file exists.
            self.get_file(path, revision)
            return True
        except Exception:
            return False

    def _cache_remote_tarball(self):
        tarball_filename = os.path.basename(self.tarball_local_path)
        if os.path.exists(self.tarball_local_path):
            # TODO check checksum and download again if does not match
            logging.debug("TarballClient: %s already cached, ignore it" %
                          tarball_filename)
            return
        try:
            request = URLRequest(self.path)

            if self.username:
                auth_string = base64.b64encode('%s:%s' % (self.username,
                                                          self.password))
                request.add_header('Authorization', 'Basic %s' % auth_string)

            with open(self.tarball_local_path, 'w') as tarball:
                tarball_data = urlopen(request).read()
                tarball.write(tarball_data)
                return
        except HTTPError as e:
            if e.code == 404:
                logging.error('404')
                raise FileNotFoundError(self.path)
            else:
                msg = "HTTP error code %d when fetching file from %s: %s" % \
                      (e.code, self.path, e)
                logging.error(msg)
                raise SCMError(msg)
        except Exception as e:
            msg = "Unexpected error fetching file from %s: %s" % (self.path, e)
            logging.error(msg)
            raise SCMError(msg)

    def _is_valid_tarfile(self):
        try:
            tarfile.open(self.tarball_local_path, mode='r')
        except:
            logging.debug('Tarball: Not a valid archive %s' %
                      self.tarball_local_path)
            os.remove(self.tarball_local_path)
            return False
        return True

    def _normalize_tarball_url(self, path):
        if path.startswith('file://') or path.startswith('http://'):
            return path
        else:
            return "file://" + path

