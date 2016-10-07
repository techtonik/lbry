import errno
import logging
import os
import shutil
import json
import sys
import tempfile


from appdirs import user_data_dir
from twisted.web import http
from twisted.web import server, static, resource
from twisted.internet import defer, error
from twisted.web.static import getTypeAndEncoding

from lbrynet.conf import UI_ADDRESS
from lbrynet.lbrynet_daemon.FileStreamer import EncryptedFileStreamer

# TODO: omg, this code is essentially duplicated in Daemon

if sys.platform != "darwin":
    data_dir = os.path.join(os.path.expanduser("~"), ".lbrynet")
else:
    data_dir = user_data_dir("LBRY")
if not os.path.isdir(data_dir):
    os.mkdir(data_dir)

log = logging.getLogger(__name__)


class NoCacheStaticFile(static.File):
    def render_GET(self, request):
        """
        Begin sending the contents of this L{File} (or a subset of the
        contents, based on the 'range' header) to the given request.
        """
        self.restat(False)
        request.setHeader('cache-control', 'no-cache, no-store, must-revalidate')
        request.setHeader('expires', '0')

        if self.type is None:
            self.type, self.encoding = getTypeAndEncoding(self.basename(),
                                                          self.contentTypes,
                                                          self.contentEncodings,
                                                          self.defaultType)

        if not self.exists():
            return self.childNotFound.render(request)

        if self.isdir():
            return self.redirect(request)

        request.setHeader(b'accept-ranges', b'bytes')

        try:
            fileForReading = self.openForReading()
        except IOError as e:
            if e.errno == errno.EACCES:
                return self.forbidden.render(request)
            else:
                raise

        if request.setLastModified(self.getModificationTime()) is http.CACHED:
            # `setLastModified` also sets the response code for us, so if the
            # request is cached, we close the file now that we've made sure that
            # the request would otherwise succeed and return an empty body.
            fileForReading.close()
            return b''

        if request.method == b'HEAD':
            # Set the content headers here, rather than making a producer.
            self._setContentHeaders(request)
            # We've opened the file to make sure it's accessible, so close it
            # now that we don't need it.
            fileForReading.close()
            return b''

        producer = self.makeProducer(request, fileForReading)
        producer.start()

        # and make sure the connection doesn't get closed
        return server.NOT_DONE_YET
    render_HEAD = render_GET


class LBRYindex(resource.Resource):
    def __init__(self, ui_dir):
        resource.Resource.__init__(self)
        self.ui_dir = ui_dir

    isLeaf = False

    def _delayed_render(self, request, results):
        request.write(str(results))
        request.finish()

    def getChild(self, name, request):
        request.setHeader('cache-control', 'no-cache, no-store, must-revalidate')
        request.setHeader('expires', '0')

        if name == '':
            return self
        return resource.Resource.getChild(self, name, request)

    def render_GET(self, request):
        return NoCacheStaticFile(os.path.join(self.ui_dir, "index.html")).render_GET(request)


class HostedEncryptedFile(resource.Resource):
    def __init__(self, api):
        self._api = api
        resource.Resource.__init__(self)

    def _make_stream_producer(self, request, stream):
        path = os.path.join(self._api.download_directory, stream.file_name)

        producer = EncryptedFileStreamer(request, path, stream, self._api.lbry_file_manager)
        request.registerProducer(producer, streaming=True)

        d = request.notifyFinish()
        d.addErrback(self._responseFailed, d)
        return d

    def render_GET(self, request):
        request.setHeader("Content-Security-Policy", "sandbox")
        if 'name' in request.args.keys():
            if request.args['name'][0] != 'lbry' and request.args['name'][0] not in self._api.waiting_on.keys():
                d = self._api._download_name(request.args['name'][0])
                d.addCallback(lambda stream: self._make_stream_producer(request, stream))
            elif request.args['name'][0] in self._api.waiting_on.keys():
                request.redirect(UI_ADDRESS + "/?watch=" + request.args['name'][0])
                request.finish()
            else:
                request.redirect(UI_ADDRESS)
                request.finish()
            return server.NOT_DONE_YET

    def _responseFailed(self, err, call):
        call.addErrback(lambda err: err.trap(error.ConnectionDone))
        call.addErrback(lambda err: err.trap(defer.CancelledError))
        call.addErrback(lambda err: log.info("Error: " + str(err)))
        call.cancel()


class EncryptedFileUpload(resource.Resource):
    """
    Accepts a file sent via the file upload widget in the web UI, saves
    it into a temporary dir, and responds with a JSON string containing
    the path of the newly created file.
    """

    def __init__(self, api):
        self._api = api

    def render_POST(self, request):
        origfilename = request.args['file_filename'][0]
        uploaded_file = request.args['file'][0]  # Temp file created by request

        # Move to a new temporary dir and restore the original file name
        newdirpath = tempfile.mkdtemp()
        newpath = os.path.join(newdirpath, origfilename)
        if os.name == "nt":
            shutil.copy(uploaded_file.name, newpath)
            # TODO Still need to remove the file

            # TODO deal with pylint error in cleaner fashion than this
            try:
                from exceptions import WindowsError as win_except
            except ImportError as e:
                log.error("This shouldn't happen")
                win_except = Exception

            try:
                os.remove(uploaded_file.name)
            except win_except as e:
                pass
        else:
            shutil.move(uploaded_file.name, newpath)
        self._api.uploaded_temp_files.append(newpath)

        return json.dumps(newpath)
