"""Routines related to PyPI, indexes"""  # FIXME:pylint:disable=too-many-lines
from __future__ import absolute_import

import logging
import os
import re
import mimetypes
import posixpath
import warnings

if True:  # pylint can't deal with the metapath magic in six.moves
    # pylint:disable=import-error
    from pip._vendor.six.moves.urllib import parse as urllib_parse
    from pip._vendor.six.moves.urllib import request as urllib_request

from pip.link import Link
from pip.finder.installed import InstalledFinder
from pip.finder.findlinks import FindLinksFinder
from pip.finder_funcs import _package_versions
from pip.found import FoundVersion
from pip.utils import (
    cached_property, normalize_name, splitext,
)
from pip.utils.deprecation import RemovedInPip7Warning
from pip.utils.logging import indent_log
from pip.exceptions import DistributionNotFound, BestVersionAlreadyInstalled
from pip.download import url_to_path, path_to_url
from pip._vendor import html5lib, requests


__all__ = ['PackageFinder']


LOCAL_HOSTNAMES = ('localhost', '127.0.0.1')
INSECURE_SCHEMES = {
    "http": ["https"],
}


logger = logging.getLogger(__name__)


class PackageFinder(object):
    """This finds packages.

    This is meant to match easy_install's technique for looking for
    packages, by reading pages and looking for appropriate links
    """
    # FIXME:pylint:disable=too-many-instance-attributes

    def __init__(self, find_links, index_urls,
                 use_wheel=True, allow_external=(), allow_unverified=(),
                 allow_all_external=False, allow_all_prereleases=False,
                 process_dependency_links=False, session=None):
        # FIXME:pylint:disable=too-many-arguments
        if session is None:
            raise TypeError(
                "PackageFinder() missing 1 required keyword argument: "
                "'session'"
            )

        self.find_links = find_links
        self.index_urls = index_urls
        self.dependency_links = []

        # These are boring links that have already been logged somehow:
        self.logged_links = set()

        self.use_wheel = use_wheel

        # Do we allow (safe and verifiable) externally hosted files?
        self.allow_external = set(normalize_name(n) for n in allow_external)

        # Which names are allowed to install insecure and unverifiable files?
        self.allow_unverified = set(
            normalize_name(n) for n in allow_unverified
        )

        # Anything that is allowed unverified is also allowed external
        self.allow_external |= self.allow_unverified

        # Do we allow all (safe and verifiable) externally hosted files?
        self.allow_all_external = allow_all_external

        # Stores if we ignored any external links so that we can instruct
        #   end users how to install them if no distributions are available
        self.need_warn_external = False

        # Stores if we ignored any unsafe links so that we can instruct
        #   end users how to install them if no distributions are available
        self.need_warn_unverified = False

        # Do we want to allow _all_ pre-releases?
        self.allow_all_prereleases = allow_all_prereleases

        # Do we process dependency links?
        self.process_dependency_links = process_dependency_links

        # The Session we'll use to make requests
        self.session = session

    def add_dependency_links(self, links):
        # # FIXME: this shouldn't be global list this, it should only
        # # apply to requirements of the package that specifies the
        # # dependency_links value
        # # FIXME: also, we should track comes_from (i.e., use Link)
        if self.process_dependency_links:
            warnings.warn(
                "Dependency Links processing has been deprecated and will be "
                "removed in a future release.",
                RemovedInPip7Warning,
            )
            self.dependency_links.extend(links)

    def _sort_locations(self, locations):
        """
        Sort locations into "files" (archives) and "urls", and return
        a pair of lists (files,urls)
        """
        files = []
        urls = []

        # puts the url for the given file path into the appropriate list
        def sort_path(path):
            url = path_to_url(path)
            if mimetypes.guess_type(url, strict=False)[0] == 'text/html':
                urls.append(url)
            else:
                files.append(url)

        for url in locations:

            is_local_path = os.path.exists(url)
            is_file_url = url.startswith('file:')
            is_find_link = url in self.find_links

            if is_local_path or is_file_url:
                if is_local_path:
                    path = url
                else:
                    path = url_to_path(url)
                if is_find_link and os.path.isdir(path):
                    path = os.path.realpath(path)
                    for item in os.listdir(path):
                        sort_path(os.path.join(path, item))
                elif is_file_url and os.path.isdir(path):
                    urls.append(url)
                elif os.path.isfile(path):
                    sort_path(path)
            else:
                urls.append(url)

        return files, urls

    def _warn_about_insecure_transport_scheme(self, logger, location):
        # These smells are enabling testability here:
        # pylint:disable=no-self-use,redefined-outer-name

        # Determine if this url used a secure transport mechanism
        parsed = urllib_parse.urlparse(str(location))
        if parsed.scheme in INSECURE_SCHEMES:
            secure_schemes = INSECURE_SCHEMES[parsed.scheme]

            if parsed.hostname in LOCAL_HOSTNAMES:
                # localhost is not a security risk
                pass
            elif len(secure_schemes) == 1:
                ctx = (location, parsed.scheme, secure_schemes[0],
                       parsed.netloc)
                logger.warn("%s uses an insecure transport scheme (%s). "
                            "Consider using %s if %s has it available" %
                            ctx)
            elif len(secure_schemes) > 1:
                ctx = (
                    location,
                    parsed.scheme,
                    ", ".join(secure_schemes),
                    parsed.netloc,
                )
                logger.warn("%s uses an insecure transport scheme (%s). "
                            "Consider using one of %s if %s has any of "
                            "them available" % ctx)
            else:
                ctx = (location, parsed.scheme)
                logger.warn("%s uses an insecure transport scheme (%s)." %
                            ctx)

    def find_requirement(self, req, upgrade):
        # FIXME:pylint:disable=too-many-locals,too-many-branches,too-many-statements

        def mkurl_pypi_url(url):
            loc = posixpath.join(url, url_name)
            # For maximum compatibility with easy_install, ensure the path
            # ends in a trailing slash.  Although this isn't in the spec
            # (and PyPI can handle it without the slash) some other index
            # implementations might break if they relied on easy_install's
            # behavior.
            if not loc.endswith('/'):
                loc = loc + '/'
            return loc

        url_name = req.url_name
        # Only check main index if index URL is given:
        main_index_url = None
        if self.index_urls:
            # Check that we have the url_name correctly spelled:
            main_index_url = Link(
                mkurl_pypi_url(self.index_urls[0]),
                trusted=True,
            )

            page = self._get_page(main_index_url, req)
            if page is None:
                url_name = self._find_url_name(
                    Link(self.index_urls[0], trusted=True),
                    url_name, req
                ) or req.url_name

        if url_name is not None:
            locations = [
                mkurl_pypi_url(url)
                for url in self.index_urls] + self.find_links
        else:
            locations = list(self.find_links)
        for version in req.absolute_versions:
            if url_name is not None and main_index_url is not None:
                locations = [
                    posixpath.join(main_index_url.url, version)] + locations

        file_locations, url_locations = self._sort_locations(locations)
        _flocations, _ulocations = self._sort_locations(self.dependency_links)
        file_locations.extend(_flocations)

        # We trust every url that the user has given us whether it was given
        #   via --index-url or --find-links
        locations = [Link(url, trusted=True) for url in url_locations]

        # We explicitly do not trust links that came from dependency_links
        locations.extend([Link(url) for url in _ulocations])

        logger.debug('URLs to search for versions for %s:', req)
        for location in locations:
            logger.debug('* %s', location)
            self._warn_about_insecure_transport_scheme(logger, location)

        found_versions = FindLinksFinder(req, self, self).found
        page_versions = []
        for page in self._get_pages(locations, req):
            logger.debug('Analyzing links from page %s', page.url)
            with indent_log():
                page_versions.extend(
                    _package_versions(page.links, req.name.lower(), self, self)
                )
        dependency_versions = list(_package_versions(
            [Link(url) for url in self.dependency_links],
            req.name.lower(), self, self,
        ))
        if dependency_versions:
            logger.debug(
                'dependency_links found: %s',
                ', '.join([
                    found.link.url for found in dependency_versions
                ])
            )
        file_versions = list(
            _package_versions(
                [Link(url) for url in file_locations],
                req.name.lower(), self, self,
            )
        )
        if (not found_versions
                and not page_versions
                and not dependency_versions
                and not file_versions):
            logger.critical(
                'Could not find any downloads that satisfy the requirement %s',
                req,
            )

            if self.need_warn_external:
                logger.warning(
                    "Some externally hosted files were ignored as access to "
                    "them may be unreliable (use --allow-external %s to "
                    "allow).",
                    req.name,
                )

            if self.need_warn_unverified:
                logger.warning(
                    "Some insecure and unverifiable files were ignored"
                    " (use --allow-unverified %s to allow).",
                    req.name,
                )

            raise DistributionNotFound(
                'No distributions at all found for %s' % req
            )
        installed_version = InstalledFinder(req).found
        if file_versions:
            file_versions = FoundVersion.sort(file_versions)
            logger.debug(
                'Local files found: %s',
                ', '.join([
                    url_to_path(found.link.url)
                    for found in file_versions
                ])
            )
        # this is an intentional priority ordering
        all_versions = installed_version + file_versions + found_versions \
            + page_versions + dependency_versions
        applicable_versions = []
        for found in all_versions:
            if found.version not in req.req:
                logger.debug(
                    "Ignoring link %s, version %s doesn't match %s",
                    found.link,
                    found.version,
                    ','.join([''.join(s) for s in req.req.specs]),
                )
                continue
            elif (found.prerelease
                  and not (self.allow_all_prereleases or req.prereleases)):
                # If this version isn't the already installed one, then
                #   ignore it if it's a pre-release.
                if not found.currently_installed:
                    logger.debug(
                        "Ignoring link %s, version %s is a pre-release (use "
                        "--pre to allow).",
                        found.link,
                        found.version,
                    )
                    continue
            applicable_versions.append(found)
        applicable_versions = FoundVersion.sort(applicable_versions)
        existing_applicable = any(
            found.currently_installed
            for found in applicable_versions
        )
        if not upgrade and existing_applicable:
            if applicable_versions.currently_installed:
                logger.debug(
                    'Existing installed version (%s) is most up-to-date and '
                    'satisfies requirement',
                    req.satisfied_by.version,
                )
            else:
                logger.debug(
                    'Existing installed version (%s) satisfies requirement '
                    '(most up-to-date version is %s)',
                    req.satisfied_by.version,
                    applicable_versions[0].version,
                )
            return None
        if not applicable_versions:
            logger.critical(
                'Could not find a version that satisfies the requirement %s '
                '(from versions: %s)',
                req,
                ', '.join(
                    sorted(set([
                        found.version
                        for found in all_versions
                    ]))),
            )

            if self.need_warn_external:
                logger.warning(
                    "Some externally hosted files were ignored as access to "
                    "them may be unreliable (use --allow-external to allow)."
                )

            if self.need_warn_unverified:
                logger.warning(
                    "Some insecure and unverifiable files were ignored"
                    " (use --allow-unverified %s to allow).",
                    req.name,
                )

            raise DistributionNotFound(
                'No distributions matching the version for %s' % req
            )
        if applicable_versions[0].currently_installed:
            # We have an existing version, and it is the best version
            logger.debug(
                'Installed version (%s) is most up-to-date (past versions: '
                '%s)',
                req.satisfied_by.version,
                ', '.join([
                    found.version for found
                    in applicable_versions[1:]
                ]) or 'none')
            raise BestVersionAlreadyInstalled
        if len(applicable_versions) > 1:
            logger.debug(
                'Using version %s (newest of versions: %s)',
                applicable_versions[0].version,
                ', '.join([
                    found.version for found
                    in applicable_versions
                ])
            )

        selected_version = applicable_versions[0].link

        if (selected_version.verifiable is not None
                and not selected_version.verifiable):
            logger.warning(
                "%s is potentially insecure and unverifiable.", req.name,
            )

        # pylint:disable=protected-access
        if selected_version._deprecated_regex:
            warnings.warn(
                "%s discovered using a deprecated method of parsing, in the "
                "future it will no longer be discovered." % req.name,
                RemovedInPip7Warning,
            )

        return selected_version

    def _find_url_name(self, index_url, url_name, req):
        """
        Finds the true URL name of a package, when the given name isn't quite
        correct.
        This is usually used to implement case-insensitivity.
        """
        if not index_url.url.endswith('/'):
            # Vaguely part of the PyPI API... weird but true.
            # FIXME: bad to modify this?
            index_url.url += '/'
        page = self._get_page(index_url, req)
        if page is None:
            logger.critical('Cannot fetch index base URL %s', index_url)
            return
        norm_name = normalize_name(req.url_name)
        for link in page.links:
            base = posixpath.basename(link.path.rstrip('/'))
            if norm_name == normalize_name(base):
                logger.debug(
                    'Real name of requirement %s is %s', url_name, base,
                )
                return base
        return None

    def _get_pages(self, locations, req):
        """
        Yields (page, page_url) from the given locations, skipping
        locations that have errors, and adding download/homepage links
        """
        all_locations = list(locations)
        seen = set()

        while all_locations:
            location = all_locations.pop(0)
            if location in seen:
                continue
            seen.add(location)

            page = self._get_page(location, req)
            if page is None:
                continue

            yield page

            for link in page.rel_links():
                normalized = normalize_name(req.name).lower()

                if (normalized not in self.allow_external
                        and not self.allow_all_external):
                    self.need_warn_external = True
                    logger.debug(
                        "Not searching %s for files because external "
                        "urls are disallowed.",
                        link,
                    )
                    continue

                if (link.trusted is not None
                        and not link.trusted
                        and normalized not in self.allow_unverified):
                    logger.debug(
                        "Not searching %s for urls, it is an "
                        "untrusted link and cannot produce safe or "
                        "verifiable files.",
                        link,
                    )
                    self.need_warn_unverified = True
                    continue

                all_locations.append(link)

    def _get_page(self, link, req):
        return HTMLPage.get_page(link, req, session=self.session)


class HTMLPage(object):
    """Represents one page, along with its URL"""

    # FIXME: these regexes are horrible hacks:
    _homepage_re = re.compile(r'<th>\s*home\s*page', re.I)
    _download_re = re.compile(r'<th>\s*download\s+url', re.I)
    _href_re = re.compile(
        'href=(?:"([^"]*)"|\'([^\']*)\'|([^>\\s\\n]*))',
        re.I | re.S
    )

    def __init__(self, content, url, headers=None, trusted=None):
        self.content = content
        self.parsed = html5lib.parse(self.content, namespaceHTMLElements=False)
        self.url = url
        self.headers = headers
        self.trusted = trusted

    def __str__(self):
        return self.url

    @classmethod
    def get_page(cls, link, req, skip_archives=True, session=None):
        # FIXME:pylint:disable=too-many-locals,too-many-branches
        if session is None:
            raise TypeError(
                "get_page() missing 1 required keyword argument: 'session'"
            )

        url = link.url
        url = url.split('#', 1)[0]

        # Check for VCS schemes that do not support lookup as web pages.
        from pip.vcs import VcsSupport
        for scheme in VcsSupport.schemes:
            if url.lower().startswith(scheme) and url[len(scheme)] in '+:':
                logger.debug('Cannot look at %s URL %s', scheme, link)
                return None

        try:
            if skip_archives:
                filename = link.filename
                for bad_ext in ['.tar', '.tar.gz', '.tar.bz2', '.tgz', '.zip']:
                    if filename.endswith(bad_ext):
                        content_type = cls._get_content_type(
                            url, session=session,
                        )
                        if content_type.lower().startswith('text/html'):
                            break
                        else:
                            logger.debug(
                                'Skipping page %s because of Content-Type: %s',
                                link,
                                content_type,
                            )
                            return

            logger.debug('Getting page %s', url)

            # Tack index.html onto file:// URLs that point to directories
            (scheme, _, path, _, _, _) = urllib_parse.urlparse(url)
            if (scheme == 'file'
                    and os.path.isdir(urllib_request.url2pathname(path))):
                # add trailing slash if not present so urljoin doesn't trim
                # final segment
                if not url.endswith('/'):
                    url += '/'
                url = urllib_parse.urljoin(url, 'index.html')
                logger.debug(' file: URL is directory, getting %s', url)

            resp = session.get(
                url,
                headers={
                    "Accept": "text/html",
                    "Cache-Control": "max-age=600",
                },
            )
            resp.raise_for_status()

            # The check for archives above only works if the url ends with
            #   something that looks like an archive. However that is not a
            #   requirement of an url. Unless we issue a HEAD request on every
            #   url we cannot know ahead of time for sure if something is HTML
            #   or not. However we can check after we've downloaded it.
            content_type = resp.headers.get('Content-Type', 'unknown')
            if not content_type.lower().startswith("text/html"):
                logger.debug(
                    'Skipping page %s because of Content-Type: %s',
                    link,
                    content_type,
                )
                return

            inst = cls(resp.text, resp.url, resp.headers, trusted=link.trusted)
        except requests.HTTPError as exc:
            level = 2 if exc.response.status_code == 404 else 1
            cls._handle_fail(req, link, exc, url, level=level)
        except requests.ConnectionError as exc:
            cls._handle_fail(
                req, link, "connection error: %s" % exc, url,
            )
        except requests.Timeout:
            cls._handle_fail(req, link, "timed out", url)
        else:
            return inst

    @staticmethod
    def _handle_fail(req, link, reason, url, level=1, meth=None):
        # pylint:disable=too-many-arguments
        del url, level
        if meth is None:
            meth = logger.debug

        meth("Could not fetch URL %s: %s", link, reason)
        meth("Will skip URL %s when looking for download links for %s",
             link.url, req)

    @staticmethod
    def _get_content_type(url, session):
        """Get the Content-Type of the given url, using a HEAD request"""
        scheme = urllib_parse.urlsplit(url)[0]
        if scheme not in ('http', 'https', 'ftp', 'ftps'):
            # FIXME: some warning or something?
            # assertion error?
            return ''

        resp = session.head(url, allow_redirects=True)
        resp.raise_for_status()

        return resp.headers.get("Content-Type", "")

    @cached_property
    def api_version(self):
        metas = [
            x for x in self.parsed.findall(".//meta")
            if x.get("name", "").lower() == "api-version"
        ]
        if metas:
            try:
                return int(metas[0].get("value", None))
            except (TypeError, ValueError):
                pass

        return None

    @cached_property
    def base_url(self):
        bases = [
            x for x in self.parsed.findall(".//base")
            if x.get("href") is not None
        ]
        if bases and bases[0].get("href"):
            return bases[0].get("href")
        else:
            return self.url

    @property
    def links(self):
        """Yields all links in the page"""
        for anchor in self.parsed.findall(".//a"):
            if anchor.get("href"):
                href = anchor.get("href")
                url = self.clean_link(
                    urllib_parse.urljoin(self.base_url, href)
                )

                # Determine if this link is internal. If that distinction
                #   doesn't make sense in this context, then we don't make
                #   any distinction.
                internal = None
                if self.api_version and self.api_version >= 2:
                    # Only api_versions >= 2 have a distinction between
                    #   external and internal links
                    internal = bool(
                        anchor.get("rel")
                        and "internal" in anchor.get("rel").split()
                    )

                yield Link(url, self, internal=internal)

    def rel_links(self):
        for url in self.explicit_rel_links():
            yield url
        for url in self.scraped_rel_links():
            yield url

    def explicit_rel_links(self, rels=('homepage', 'download')):
        """Yields all links with the given relations"""
        rels = set(rels)

        for anchor in self.parsed.findall(".//a"):
            if anchor.get("rel") and anchor.get("href"):
                found_rels = set(anchor.get("rel").split())
                # Determine the intersection between what rels were found and
                #   what rels were being looked for
                if found_rels & rels:
                    href = anchor.get("href")
                    url = self.clean_link(
                        urllib_parse.urljoin(self.base_url, href)
                    )
                    yield Link(url, self, trusted=False)

    def scraped_rel_links(self):
        # Can we get rid of this horrible horrible method?
        for regex in (self._homepage_re, self._download_re):
            match = regex.search(self.content)
            if not match:
                continue
            href_match = self._href_re.search(self.content, pos=match.end())
            if not href_match:
                continue
            url = (
                href_match.group(1)
                or href_match.group(2)
                or href_match.group(3)
            )
            if not url:
                continue
            url = self.clean_link(urllib_parse.urljoin(self.base_url, url))
            yield Link(url, self, trusted=False, _deprecated_regex=True)

    _clean_re = re.compile(r'[^a-z0-9$&+,/:;=?@.#%_\\|-]', re.I)

    def clean_link(self, url):
        """Makes sure a link is fully encoded.  That is, if a ' ' shows up in
        the link, it will be rewritten to %20 (while not over-quoting
        % or other characters)."""
        return self._clean_re.sub(
            lambda match: '%%%2x' % ord(match.group(0)), url)


def get_requirement_from_url(url):
    """Get a requirement from the URL, if possible.  This looks for #egg
    in the URL"""
    link = Link(url)
    egg_info = link.egg_fragment
    if not egg_info:
        egg_info = splitext(link.filename)[0]
    return package_to_requirement(egg_info)


def package_to_requirement(package_name):
    """Translate a name like Foo-1.2 to Foo==1.3"""
    match = re.search(r'^(.*?)-(dev|\d.*)', package_name)
    if match:
        name = match.group(1)
        version = match.group(2)
    else:
        name = package_name
        version = ''
    if version:
        return '%s==%s' % (name, version)
    else:
        return name
