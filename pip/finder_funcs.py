import logging
import re
import sys

if True:  # pylint can't deal with the metapath magic in six.moves
    # pylint:disable=no-name-in-module,import-error
    from pip._vendor.six.moves.urllib import parse as urllib_parse

from pip.exceptions import InvalidWheelFilename
from pip.found import FoundVersion
from pip.utils import normalize_name
from pip.wheel import Wheel, wheel_ext
from pip.pep425tags import supported_tags_noarch, get_platform

logger = logging.getLogger(__name__)

# functions sorted by depth-first dependency order


def _sort_links(links):
    """
    Returns elements of links in order, non-egg links first, egg links
    second, while eliminating duplicates
    """
    eggs, no_eggs = [], []
    seen = set()
    for link in links:
        if link not in seen:
            seen.add(link)
            if link.egg_fragment:
                eggs.append(link)
            else:
                no_eggs.append(link)
    return no_eggs + eggs


_egg_info_re = re.compile(r'([a-z0-9_.]+)-([a-z0-9_.-]+)', re.I)
_py_version_re = re.compile(r'-py([123]\.?[0-9]?)$')


def _egg_info_matches(egg_info, search_name, link):
    match = _egg_info_re.search(egg_info)
    if not match:
        logger.debug('Could not parse version from link: %s', link)
        return None
    name = match.group(0).lower()
    # To match the "safe" name that pkg_resources creates:
    name = name.replace('_', '-')
    # project name and version must be separated by a dash
    look_for = search_name.lower() + "-"
    if name.startswith(look_for):
        return match.group(0)[len(look_for):]
    else:
        return None


def _known_extensions(use_wheel):
    extensions = ('.tar.gz', '.tar.bz2', '.tar', '.tgz', '.zip')
    if use_wheel:
        return extensions + (wheel_ext,)
    return extensions


def _link_package_versions(link, search_name, config, state):
    """
    Return an iterable of triples (pkg_resources_version_key,
    link, python_version) that can be extracted from the given
    link.

    Meant to be overridden by subclasses, not called by clients.
    logged_links
    """
    # FIXME:pylint:disable=too-many-return-statements,too-many-branches,too-many-statements
    platform = get_platform()

    version = None
    if link.egg_fragment:
        egg_info = link.egg_fragment
    else:
        egg_info, ext = link.splitext()
        if not ext:
            if link not in state.logged_links:
                logger.debug('Skipping link %s; not a file', link)
                state.logged_links.add(link)
            return []
        if egg_info.endswith('.tar'):
            # Special double-extension case:
            egg_info = egg_info[:-4]
            ext = '.tar' + ext
        if ext not in _known_extensions(config.use_wheel):
            if link not in state.logged_links:
                logger.debug(
                    'Skipping link %s; unknown archive format: %s',
                    link,
                    ext,
                )
                state.logged_links.add(link)
            return []
        if "macosx10" in link.path and ext == '.zip':
            if link not in state.logged_links:
                logger.debug('Skipping link %s; macosx10 one', link)
                state.logged_links.add(link)
            return []
        if ext == wheel_ext:
            try:
                wheel = Wheel(link.filename)
            except InvalidWheelFilename:
                logger.debug(
                    'Skipping %s because the wheel filename is invalid',
                    link
                )
                return []
            if wheel.name.lower() != search_name.lower():
                logger.debug(
                    'Skipping link %s; wrong project name (not %s)',
                    link,
                    search_name,
                )
                return []
            if not wheel.supported():
                logger.debug(
                    'Skipping %s because it is not compatible with this '
                    'Python',
                    link,
                )
                return []
            # This is a dirty hack to prevent installing Binary Wheels from
            # PyPI unless it is a Windows or Mac Binary Wheel. This is
            # paired with a change to PyPI disabling uploads for the
            # same. Once we have a mechanism for enabling support for
            # binary wheels on linux that deals with the inherent problems
            # of binary distribution this can be removed.
            comes_from = getattr(link, "comes_from", None)
            if (
                    (
                        not platform.startswith('win')
                        and not platform.startswith('macosx')
                        and not platform == 'cli'
                    )
                    and comes_from is not None
                    and urllib_parse.urlparse(
                        comes_from.url
                    ).netloc.endswith("pypi.python.org")):
                if not wheel.supported(tags=supported_tags_noarch):
                    logger.debug(
                        "Skipping %s because it is a pypi-hosted binary "
                        "Wheel on an unsupported platform",
                        link,
                    )
                    return []
            version = wheel.version

    if not version:
        version = _egg_info_matches(egg_info, search_name, link)
    if version is None:
        logger.debug(
            'Skipping link %s; wrong project name (not %s)',
            link,
            search_name,
        )
        return []

    if (link.internal is not None
            and not link.internal
            and not normalize_name(search_name).lower()
            in config.allow_external
            and not config.allow_all_external):
        # We have a link that we are sure is external, so we should skip
        #   it unless we are allowing externals
        logger.debug("Skipping %s because it is externally hosted.", link)
        state.need_warn_external = True
        return []

    if (link.verifiable is not None
            and not link.verifiable
            and not (normalize_name(search_name).lower()
                     in config.allow_unverified)):
        # We have a link that we are sure we cannot verify its integrity,
        #   so we should skip it unless we are allowing unsafe installs
        #   for this requirement.
        logger.debug(
            "Skipping %s because it is an insecure and unverifiable file.",
            link,
        )
        state.need_warn_unverified = True
        return []

    match = _py_version_re.search(version)
    if match:
        version = version[:match.start()]
        py_version = match.group(1)
        if py_version != sys.version[:3]:
            logger.debug(
                'Skipping %s because Python version is incorrect', link
            )
            return []
    logger.debug('Found link %s, version: %s', link, version)
    return [FoundVersion(version, link)]


def _package_versions(links, search_name, config, state):
    for link in _sort_links(links):
        for v in _link_package_versions(link, search_name, config, state):
            yield v
