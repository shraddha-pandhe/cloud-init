# vi: ts=4 expandtab
#
#    Copyright (C) 2012 Canonical Ltd.
#    Copyright (C) 2012 Hewlett-Packard Development Company, L.P.
#    Copyright (C) 2012 Yahoo! Inc.
#
#    Author: Scott Moser <scott.moser@canonical.com>
#    Author: Juerg Haefliger <juerg.haefliger@hp.com>
#    Author: Joshua Harlow <harlowja@yahoo-inc.com>
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License version 3, as
#    published by the Free Software Foundation.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.

import time

import requests
from requests import exceptions

from urlparse import (urlparse, urlunparse)

from cloudinit import log as logging
from cloudinit import version

LOG = logging.getLogger(__name__)

# Check if requests has ssl support (added in requests >= 0.8.8)
SSL_ENABLED = False
CONFIG_ENABLED = False # This was added in 0.7
try:
    import pkg_resources
    from distutils.version import LooseVersion
    _REQ = pkg_resources.get_distribution('requests')
    _REQ_VER = LooseVersion(_REQ.version)
    if _REQ_VER >= LooseVersion('0.8.8'):
        SSL_ENABLED = True
    if _REQ_VER >= LooseVersion('0.7.0'):
        CONFIG_ENABLED = True
except:
    pass


def _cleanurl(url):
    parsed_url = list(urlparse(url, scheme='http'))
    if not parsed_url[1] and parsed_url[2]:
        # Swap these since this seems to be a common
        # occurrence when given urls like 'www.google.com'
        parsed_url[1] = parsed_url[2]
        parsed_url[2] = ''
    return urlunparse(parsed_url)


def readurl(url, data=None, timeout=None, retries=0, sec_between=1,
            headers=None, ssl_details=None, check_status=True,
            allow_redirects=False):
    url = _cleanurl(url)
    req_args = {
        'url': url,
    }
    if urlparse(url).scheme == 'https' and ssl_details:
        if not SSL_ENABLED:
            LOG.warn("SSL is not enabled, cert. verification can not occur!")
        else:
            if 'ca_certs' in ssl_details and ssl_details['ca_certs']:
                req_args['verify'] = ssl_details['ca_certs']
            else:
                req_args['verify'] = True
            if 'cert_file' in ssl_details and 'key_file' in ssl_details:
                req_args['cert'] = [ssl_details['cert_file'],
                                    ssl_details['key_file']]
                                    
    req_args['allow_redirects'] = allow_redirects
    req_args['method'] = 'GET'
    if timeout is not None:
        req_args['timeout'] = max(float(timeout), 0)
    if data:
        req_args['method'] = 'POST'
    # It doesn't seem like config
    # was added in older library versions, thus we
    # need to manually do the retries if it wasn't
    if CONFIG_ENABLED:
        req_config = {
            'store_cookies': False,
        }
        # Don't use the retry support built-in
        # since it doesn't allow for 'sleep_times'
        # in between tries....
        # if retries:
        #     req_config['max_retries'] = max(int(retries), 0)
        req_args['config'] = req_config
    manual_tries = 1
    if retries:
        manual_tries = max(int(retries) + 1, 1)
    if not headers:
        headers = {
            'User-Agent': 'Cloud-Init/%s' % (version.version_string()),
        }
    req_args['headers'] = headers
    LOG.debug("Attempting to open '%s' with %s configuration", url, req_args)
    if data:
        # Do this after the log (it might be large)
        req_args['data'] = data
    if sec_between is None:
        sec_between = -1
    excps = []
    # Handle retrying ourselves since the built-in support
    # doesn't handle sleeping between tries...
    for i in range(0, manual_tries):
        try:
            r = requests.request(**req_args)
            if check_status:
                r.raise_for_status()
            contents = r.content
            status = r.status_code
            headers = r.headers
            LOG.debug("Read from %s (%s, %sb) after %s attempts", url,
                      status, len(contents), (i + 1))
            # Doesn't seem like we can make it use a different
            # subclass for responses, so add our own backward-compat
            # attrs
            if not hasattr(r, 'code'):
                setattr(r, 'code', status)
            if not hasattr(r, 'contents'):
                setattr(r, 'contents', contents)
            return r
        except exceptions.RequestException as e:
            excps.append(e)
            if i + 1 < manual_tries and sec_between > 0:
                LOG.debug("Please wait %s seconds while we wait to try again",
                          sec_between)
                time.sleep(sec_between)
    if excps:
        raise excps[-1]
    return None # Should throw before this...


def wait_for_url(urls, max_wait=None, timeout=None,
                 status_cb=None, headers_cb=None, sleep_time=1,
                 exception_cb=None):
    """
    urls:      a list of urls to try
    max_wait:  roughly the maximum time to wait before giving up
               The max time is *actually* len(urls)*timeout as each url will
               be tried once and given the timeout provided.
    timeout:   the timeout provided to urlopen
    status_cb: call method with string message when a url is not available
    headers_cb: call method with single argument of url to get headers
                for request.
    exception_cb: call method with 2 arguments 'msg' (per status_cb) and
                  'exception', the exception that occurred.

    the idea of this routine is to wait for the EC2 metdata service to
    come up.  On both Eucalyptus and EC2 we have seen the case where
    the instance hit the MD before the MD service was up.  EC2 seems
    to have permenantely fixed this, though.

    In openstack, the metadata service might be painfully slow, and
    unable to avoid hitting a timeout of even up to 10 seconds or more
    (LP: #894279) for a simple GET.

    Offset those needs with the need to not hang forever (and block boot)
    on a system where cloud-init is configured to look for EC2 Metadata
    service but is not going to find one.  It is possible that the instance
    data host (169.254.169.254) may be firewalled off Entirely for a sytem,
    meaning that the connection will block forever unless a timeout is set.
    """
    start_time = time.time()

    def log_status_cb(msg, exc=None):
        LOG.debug(msg)

    if status_cb is None:
        status_cb = log_status_cb

    def timeup(max_wait, start_time):
        return ((max_wait <= 0 or max_wait is None) or
                (time.time() - start_time > max_wait))

    loop_n = 0
    while True:
        sleep_time = int(loop_n / 5) + 1
        for url in urls:
            now = time.time()
            if loop_n != 0:
                if timeup(max_wait, start_time):
                    break
                if timeout and (now + timeout > (start_time + max_wait)):
                    # shorten timeout to not run way over max_time
                    timeout = int((start_time + max_wait) - now)

            reason = ""
            try:
                if headers_cb is not None:
                    headers = headers_cb(url)
                else:
                    headers = {}

                resp = readurl(url, headers=headers, timeout=timeout,
                               check_status=False)
                if not resp.contents:
                    reason = "empty response [%s]" % (resp.code)
                    e = ValueError(reason)
                elif not resp.ok():
                    reason = "bad status code [%s]" % (resp.code)
                    e = ValueError(reason)
                else:
                    return url
            except exceptions.RequestException as e:
                reason = "request error [%s]" % e
            except Exception as e:
                reason = "unexpected error [%s]" % e

            time_taken = int(time.time() - start_time)
            status_msg = "Calling '%s' failed [%s/%ss]: %s" % (url,
                                                             time_taken,
                                                             max_wait, reason)
            status_cb(status_msg)
            if exception_cb:
                exception_cb(msg=status_msg, exception=e)

        if timeup(max_wait, start_time):
            break

        loop_n = loop_n + 1
        LOG.debug("Please wait %s seconds while we wait to try again",
                  sleep_time)
        time.sleep(sleep_time)

    return False
