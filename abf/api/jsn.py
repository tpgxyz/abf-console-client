import urllib2, urllib
import re
import json
import os
import base64
import pdb
import uuid
import tempfile
import httplib
import mimetypes
import base64
import sha
import shutil

from abf.api.exceptions import *


from beaker.cache import Cache
from beaker.util import parse_cache_config_options
cache_data = Cache('abf_etag_data', expire = 86400, type='file', data_dir='/tmp/abf_cache/etag_data', lock_dir='/tmp/abf_cache/etag_data')
cache_etags = Cache('abf_etags', expire = 86400, type='file', data_dir='/tmp/abf_cache/etags', lock_dir='/tmp/abf_cache/etags')

SYMBOLS = {
    'basic'     : ('b', 'k', 'm', 'g', 't'),
    'basic_long' : ('byte', 'kilo', 'mega', 'giga', 'tera'),
    'iec'           : ('bi', 'ki', 'mi', 'gi', 'ti'),
    'iec_long'       : ('byte', 'kibi', 'mebi', 'gibi', 'tebi'),
}


def bytes2human(n, format='%(value).1f%(symbol)s', symbols='basic'):
    n = int(n)
    if n < 0:
        raise ValueError("n < 0 (%s)" % str(n))
    title = not symbols.endswith('_long')
    symbols = SYMBOLS[symbols]
    prefix = {}
    for i, s in enumerate(symbols[1:]):
        prefix[s] = 1 << (i+1)*10
        
    for symbol in reversed(symbols[1:]):
        if n >= prefix[symbol]:
            value = float(n) / prefix[symbol]
            symbol = (title and symbol.title()) or symbol
            return format % locals()
    return format % dict(symbol=(title and symbols[0].title()) or symbols[0], value=n)

class AbfJson(object):
    def __init__(self, abf_url, file_store_url, login, password, log):
        self.login = login
        self.password = password
        self.abf_url = re.compile('/+$').sub('', abf_url)
        self.file_store_url = re.compile('/+$').sub('', file_store_url)
        
        if not self.file_store_url.startswith('http://'):
            log.error('File-store url have to start with "http://"')
            exit(1)
        
        self.file_store_domain = self.file_store_url[7:]
        
        #does not work sometimes
        '''self.password_manager = urllib2.HTTPPasswordMgrWithDefaultRealm()
        self.password_manager.add_password(None, abf_url, login, password)
        self.auth = urllib2.HTTPBasicAuthHandler(self.password_manager) 
        self.opener = urllib2.build_opener(self.auth)
        urllib2.install_opener(self.opener)
        '''
        
        # but it works!
        self.base64_auth_string = base64.standard_b64encode('%s:%s' % (login, password)).replace('\n', '')
        self.log = log
        
    errors = {
        "Invalid email or password.": AuthError,
        "403 Forbidden | Rate Limit Exceeded": RateLimitError,
        "Page not found": PageNotFoundError,
        "Error 404. Resource not found!": PageNotFoundError,
        "Something went wrong. We've been notified about this issue and we'll take a look at it shortly.": InternalServerError,
        "We update the site, it will take some time. We are really trying to do it fast. We apologize for any inconvenience..": ServerWorksError,
        "Requires authentication": AuthError,
        "Forbidden. Sorry, you don't have enough rights for this action!": ForbiddenError,
        "Access violation to this page!": ForbiddenError,
        "Bad Request": BadRequestError,}
    good_messages = ['Errors during build publishing!', 'Build is queued for publishing']
        
    # in case of this error, get_url_contents will print the error message and exit
    fatal_errors = [AuthError, RateLimitError, InternalServerError, ServerWorksError]
    
    def process_response(self, response_sting):
        try:
            res = json.loads(response_sting)
        except ValueError, ex:
            self.log.error("Internal server error: it has returned non-json data. ")
            exit(1)
        m = None
        if 'message' in res and res['message'] not in AbfJson.good_messages:
            m = res['message']
        if 'error' in res:
            m = res['error']
        if 'Error' in res:
            m = res['Error']
        if m:
            if m in AbfJson.errors:
                self.log.debug(m)
                exception = AbfJson.errors[m]
            else:
                self.log.error("Unknown server error: " + m)
                exception = AbfApiException
            if exception == BadRequestError:
                log.error('Sorry, but something went wrong and request I\'ve sent to ABF is bad. Please, '
                    'notify developers, send them a set of command-line arguments and the request data:\n%s\n%s' % (URL, post_json or "No POST DATA") )
                exit(1)
                
            if exception in AbfJson.fatal_errors:
                exit(2)
            raise exception(m)
        return res
    
    def get_url_contents(self, path, GET=None, POST=None, file_store=False):
        url = ((file_store and self.file_store_url) or self.abf_url) + path
        if GET:
            get_string = urllib.urlencode(GET)
            if '?' in url:
                url = url + '&' + get_string
            else:
                url = url + '?' + get_string
        self.log.debug('Fetching url %s' % url)

        etag = None
        if POST:
            post_json = json.dumps(POST).encode('utf-8')
            request = urllib2.Request(url, post_json, {'Content-Type': 'application/json'})
        else:
            request = urllib2.Request(url)
            
            if cache_etags.has_key(url):
                etag = cache_etags.get(url)
                if cache_data.has_key(etag):
                    self.log.debug("It was cached! ETag: " + etag)
                    request.add_header("If-None-Match", etag) 
                
        
        request.add_header("Authorization", "Basic %s" % self.base64_auth_string)  
        etag_new = None
        try:
            result = urllib2.urlopen(request)
            res = result.read()
            etag_new = result.headers.getheaders('ETag')[0]
        except urllib2.HTTPError, ex:
            if ex.code == 304: # data was not modified
                res = cache_data.get(etag)
                self.log.debug('Getting cached result (cache was validated)')
            else:
                self.log.debug('Return code: ' + str(ex.code))

                if ex.code == 401: # auth failed
                    self.log.error("Authorization failed. Incorrect username or password")
                    exit()
                    
                #remove cached data if exists
                if etag:
                    try:
                        cache_etags.remove(url)
                        cache_data.remove(etag)
                    except:
                        pass
                res = ex.fp.read()
                
        if etag_new:
            self.log.debug("Caching the new value for %s. ETag is %s" % (url, etag_new))
            cache_etags.put(url, etag_new)
            cache_data.put(etag_new, res)
            
        res = self.process_response(res)
  
        #print 'RAW OUTPUT', res
        return res
      


    MAX_SIZE = 32 * 1024 * 1024
    BLOCK_SIZE = 1024 * 1024

    @staticmethod
    def __encode_multipart_formdata(body, boundary, fields = [], files = []):
        for key, value in fields:
            body.write('--%s\r\n' % boundary)

            body.write('Content-Disposition: form-data; name="%s"\r\n' % key)
            body.write('Content-Type: text/plain\r\n\r\n')

            body.write(value)

            body.write('\r\n')

        for key, value in files:
            content_type = mimetypes.guess_type(value)[0] or 'application/octet-stream'

            body.write('--%s\r\n' % boundary)

            body.write('Content-Disposition: form-data; name="%s"; filename="%s"\r\n' % (key, value))
            body.write('Content-Type: %s\r\n\r\n' % content_type)

            fobj = open(value, 'rb')

            
            datablock = 1
            while datablock:
                datablock = fobj.read(AbfJson.BLOCK_SIZE)
                if datablock:
                    body.write(datablock)

            fobj.close()

            body.write('\r\n')

        body.write('--%s--\r\n' % boundary)  
        
    def compute_sha1(self, file_name):
        fd = open(file_name, 'rb')
        datablock = 1
        s = sha.new()
        while datablock:
            datablock = fd.read(AbfJson.BLOCK_SIZE)
            if datablock:
                s.update(datablock)
        hex_sha = s.hexdigest()
        return hex_sha
        
    def upload_file(self, file_name, silent=False):
        self.log.debug('Looking for "%s" in file-store...' % file_name)
        sha_hash = self.compute_sha1(file_name)
        self.log.debug('File hash is %s' % sha_hash)
        
        res = self.get_file_info_by_hash(sha_hash)
        if res:
            fn = res[0]['file_name']
            sha_hash_new = res[0]['sha1_hash']
            if sha_hash_new != sha_hash:
                self.log.critical('File-Store returned file for sha1 %s instead of %s!' % (sha_hash_new, sha_hash))
                exit(1)
            new_fn = os.path.basename(file_name)
            if fn != new_fn and not silent:
                self.log.warning('The name of the file in file-store is %s, but you are trying to upload file %s' % (fn, new_fn))
            return sha_hash
        
        tempfile.tempdir = '/tmp'

        boundary = uuid.uuid4().hex
        
        body = tempfile.SpooledTemporaryFile(max_size = AbfJson.MAX_SIZE)
        self.__encode_multipart_formdata(body, boundary,[], [('file_store[file]', file_name)])
        length = body.tell()
        body.seek(0)
        if not silent:
            self.log.info('Uploading %s (%s)' % (file_name, bytes2human(os.stat(file_name).st_size)))
        conn = httplib.HTTPConnection(self.file_store_domain, 80)
        content_type = 'multipart/form-data; boundary=%s' % boundary
        headers = {'Content-Type' : content_type, 'Content-Length' : length, "Authorization": "Basic %s" % self.base64_auth_string}
        conn.request('POST', '/api/v1/upload', body, headers)
        body.close()
        
        resp = conn.getresponse()
        output = resp.read()
        conn.close()
        if resp.status < 200 or resp.status > 299:
            self.log.error("Could not upload file. HTTP error %s %s" %  (resp.status, resp.reason))
            exit(1)
            
        output = json.loads(output)
        return output['sha1_hash'] or None
        
        
    def fetch_file(self, sha_hash, path):
        URL = self.file_store_url + '/api/v1/file_stores/' + sha_hash
        try:
            response = urllib2.urlopen(URL)
        except urllib2.HTTPError, ex:
            if ex.code == 404: # data was not modified
                raise PageNotFoundError('File with hash %s can not be downloaded from File-Store.' % sha_hash)
            else:
                raise AbfApiException('Error while downloading file by hash %s: %s' % (sha_hash, str(ex)))
        fd = open(path, 'wb')
        shutil.copyfileobj(response, fd)
        fd.close()
    
    def get_file_info_by_hash(self, sha_hash):
        URL = "/api/v1/file_stores.json"
        GET = {'hash': sha_hash}
        return self.get_url_contents(URL, GET=GET, file_store=True)

    def get_architectures(self):
        URL = "/api/v1/arches.json"
        return self.get_url_contents(URL)
    
    def get_platforms(self, typ=None):
        URL = "/api/v1/platforms.json"
        GET = {}
        if typ:
            GET['type'] = typ
        return self.get_url_contents(URL, GET)
        
    def get_platform_by_id(self, pl_id):
        pl_id = int(pl_id)
        URL = "/api/v1/platforms/%d.json" % pl_id
        return self.get_url_contents(URL)
        
    def get_build_platforms(self):
        URL = "/api/v1/platforms/platforms_for_build.json" 
        return self.get_url_contents(URL)
        
    def get_repository_by_id(self, rep_id):
        rep_id = int(rep_id)
        URL = "/api/v1/repositories/%d.json" % rep_id
        return self.get_url_contents(URL)
        
    def get_buildlist_by_id(self, bl_id):
        bl_id = int(bl_id)
        URL = "/api/v1/build_lists/%d.json" % bl_id
        return self.get_url_contents(URL)
        
    def get_project_by_id(self, p_id):
        p_id = int(p_id)
        URL = "/api/v1/projects/%d.json" % p_id
        return self.get_url_contents(URL)
        
    def get_project_id_by_name(self, key):
        proj_owner, proj_name = key
        URL = "/api/v1/projects/get_id.json"
        GET = {'name': proj_name, 'owner':proj_owner}
        return self.get_url_contents(URL, GET)
        
    def new_build_task(self, data):
        URL = "/api/v1/build_lists.json"
        return self.get_url_contents(URL, GET=None, POST=data)
        
    def publish(self, task_id):
        task_id = int(task_id)
        URL = "/api/v1/build_lists/%d/publish.json" % task_id
        return self.get_url_contents(URL)
        
    def get_git_refs_list(self, proj_id):
        proj_id = int(proj_id)
        URL = "/api/v1/projects/%d/refs_list.json"  % proj_id
        return self.get_url_contents(URL)
        
    def get_user_by_id(self, user_id):
        user_id = int(user_id)
        URL = "/api/v1/users/%d.json" % user_id
        return self.get_url_contents(URL)
        
    def get_group_by_id(self, group_id):
        group_id = int(group_id)
        URL = "/api/v1/groups/%d.json" % group_id
        return self.get_url_contents(URL)
        
    def get_search_results(self, search_type, query):
        URL = "/api/v1/search.json"
        GET = {'type': search_type, 'query': query, 'per_page': 100}
        return self.get_url_contents(URL, GET=GET)