""" ConanFile user tools, as download, etc
"""
from __future__ import print_function

import logging
import multiprocessing
import os
import platform
import sys
from contextlib import contextmanager

import requests
from patch import fromfile, fromstring

from conans.client.output import ConanOutput
from conans.client.rest.uploader_downloader import Downloader
from conans.client.runner import ConanRunner
from conans.errors import ConanException
from conans.model.version import Version
from conans.util.files import _generic_algorithm_sum, load
from conans.util.log import logger


@contextmanager
def pythonpath(conanfile):
    old_path = sys.path[:]

    simple_vars, multiple_vars = conanfile.env_values_dicts
    python_path = multiple_vars.get("PYTHONPATH", None) or [simple_vars.get("PYTHONPATH", None)]
    if python_path:
        sys.path.extend(python_path)

    yield
    sys.path = old_path


@contextmanager
def environment_append(env_vars, list_env_vars=None):
    """
    :param env_vars: List of simple environment vars. {name: value, name2: value2} => e.j: MYVAR=1
    :param list_env_vars: List of appendable environment vars. {name: [value, value2]} => e.j. PATH=/path/1:/path/2
    :return: None
    """
    old_env = dict(os.environ)
    if list_env_vars:
        for name, value in list_env_vars.items():
            env_vars[name] = os.pathsep.join(value)
            if name in old_env:
                env_vars[name] += os.pathsep + old_env[name]
    os.environ.update(env_vars)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(old_env)


def build_sln_command(settings, sln_path, targets=None, upgrade_project=True):
    """
    Use example:
        build_command = build_sln_command(self.settings, "myfile.sln", targets=["SDL2_image"])
        env = ConfigureEnvironment(self)
        command = "%s && %s" % (env.command_line_env, build_command)
        self.run(command)
    """
    targets = targets or []
    command = "devenv %s /upgrade && " % sln_path if upgrade_project else ""
    command += "msbuild %s /p:Configuration=%s" % (sln_path, settings.build_type)
    if str(settings.arch) in ["x86_64", "x86"]:
        command += ' /p:Platform='
        command += '"x64"' if settings.arch == "x86_64" else '"x86"'
    elif "ARM" in str(settings.arch).upper():
        command += ' /p:Platform="ARM"'

    if targets:
        command += " /target:%s" % ";".join(targets)
    return command


def vcvars_command(settings):
    param = "x86" if settings.arch == "x86" else "amd64"
    existing_version = os.environ.get("VisualStudioVersion")
    if existing_version:
        command = ""
        existing_version = existing_version.split(".")[0]
        if existing_version != settings.compiler.version:
            raise ConanException("Error, Visual environment already set to %s\n"
                                 "Current settings visual version: %s"
                                 % (existing_version, settings.compiler.version))
    else:
        env_var = "vs%s0comntools" % settings.compiler.version
        try:
            vs_path = os.environ[env_var]
        except KeyError:
            raise ConanException("VS '%s' variable not defined. Please install VS or define "
                                 "the variable (VS2017)" % env_var)
        if settings.compiler.version != "15":
            command = ('call "%s../../VC/vcvarsall.bat" %s' % (vs_path, param))
        else:
            command = ('call "%s../../VC/Auxiliary/Build/vcvarsall.bat" %s' % (vs_path, param))
    return command


def cpu_count():
    try:
        return multiprocessing.cpu_count()
    except NotImplementedError:
        print("WARN: multiprocessing.cpu_count() not implemented. Defaulting to 1 cpu")
    return 1  # Safe guess


def human_size(size_bytes):
    """
    format a size in bytes into a 'human' file size, e.g. bytes, KB, MB, GB, TB, PB
    Note that bytes/KB will be reported in whole numbers but MB and above will have
    greater precision.  e.g. 1 byte, 43 bytes, 443 KB, 4.3 MB, 4.43 GB, etc
    """
    if size_bytes == 1:
        return "1 byte"

    suffixes_table = [('bytes', 0), ('KB', 0), ('MB', 1), ('GB', 2), ('TB', 2), ('PB', 2)]

    num = float(size_bytes)
    for suffix, precision in suffixes_table:
        if num < 1024.0:
            break
        num /= 1024.0

    if precision == 0:
        formatted_size = "%d" % num
    else:
        formatted_size = str(round(num, ndigits=precision))

    return "%s %s" % (formatted_size, suffix)


def unzip(filename, destination=".", keep_permissions=False):
    """
    Unzip a zipped file
    :param filename: Path to the zip file
    :param destination: Destination folder
    :param keep_permissions: Keep the zip permissions. WARNING: Can be dangerous if the zip was not created in a NIX
    system, the bits could produce undefined permission schema. Use only this option if you are sure that the
    zip was created correctly.
    :return:
    """
    if (filename.endswith(".tar.gz") or filename.endswith(".tgz") or
        filename.endswith(".tbz2") or filename.endswith(".tar.bz2") or
            filename.endswith(".tar")):
        return untargz(filename, destination)
    import zipfile
    full_path = os.path.normpath(os.path.join(os.getcwd(), destination))

    if hasattr(sys.stdout, "isatty") and sys.stdout.isatty():
        def print_progress(extracted_size, uncompress_size):
            txt_msg = "Unzipping %.0f %%\r" % (extracted_size * 100.0 / uncompress_size)
            print(txt_msg, end='')
    else:
        def print_progress(extracted_size, uncompress_size):
            pass

    with zipfile.ZipFile(filename, "r") as z:
        uncompress_size = sum((file_.file_size for file_ in z.infolist()))
        print("Unzipping %s, this can take a while" % human_size(uncompress_size))
        extracted_size = 0
        if platform.system() == "Windows":
            for file_ in z.infolist():
                extracted_size += file_.file_size
                print_progress(extracted_size, uncompress_size)
                try:
                    # Win path limit is 260 chars
                    if len(file_.filename) + len(full_path) >= 260:
                        raise ValueError("Filename too long")
                    z.extract(file_, full_path)
                except Exception as e:
                    print("Error extract %s\n%s" % (file_.filename, str(e)))
        else:  # duplicated for, to avoid a platform check for each zipped file
            for file_ in z.infolist():
                extracted_size += file_.file_size
                print_progress(extracted_size, uncompress_size)
                try:
                    z.extract(file_, full_path)
                    if keep_permissions:
                        # Could be dangerous if the ZIP has been created in a non nix system
                        # https://bugs.python.org/issue15795
                        perm = file_.external_attr >> 16 & 0xFFF
                        os.chmod(os.path.join(full_path, file_.filename), perm)
                except Exception as e:
                    print("Error extract %s\n%s" % (file_.filename, str(e)))


def untargz(filename, destination="."):
    import tarfile
    with tarfile.TarFile.open(filename, 'r:*') as tarredgzippedFile:
        tarredgzippedFile.extractall(destination)


def get(url):
    """ high level downloader + unziper + delete temporary zip
    """
    filename = os.path.basename(url)
    download(url, filename)
    unzip(filename)
    os.unlink(filename)


def download(url, filename, verify=True, out=None, retry=2, retry_wait=5):
    out = out or ConanOutput(sys.stdout, True)
    if verify:
        # We check the certificate using a list of known verifiers
        import conans.client.rest.cacert as cacert
        verify = cacert.file_path
    downloader = Downloader(requests, out, verify=verify)
    downloader.download(url, filename, retry=retry, retry_wait=retry_wait)
    out.writeln("")
#     save(filename, content)


def replace_in_file(file_path, search, replace):
    content = load(file_path)
    content = content.replace(search, replace)
    content = content.encode("utf-8")
    with open(file_path, "wb") as handle:
        handle.write(content)


def check_with_algorithm_sum(algorithm_name, file_path, signature):

    real_signature = _generic_algorithm_sum(file_path, algorithm_name)
    if real_signature != signature:
        raise ConanException("%s signature failed for '%s' file."
                             " Computed signature: %s" % (algorithm_name,
                                                          os.path.basename(file_path),
                                                          real_signature))


def check_sha1(file_path, signature):
    check_with_algorithm_sum("sha1", file_path, signature)


def check_md5(file_path, signature):
    check_with_algorithm_sum("md5", file_path, signature)


def check_sha256(file_path, signature):
    check_with_algorithm_sum("sha256", file_path, signature)


def patch(base_path=None, patch_file=None, patch_string=None, strip=0, output=None):
    """Applies a diff from file (patch_file)  or string (patch_string)
    in base_path directory or current dir if None"""

    class PatchLogHandler(logging.Handler):
        def __init__(self):
            logging.Handler.__init__(self, logging.DEBUG)
            self.output = output or ConanOutput(sys.stdout, True)
            self.patchname = patch_file if patch_file else "patch"

        def emit(self, record):
            logstr = self.format(record)
            if record.levelno == logging.WARN:
                self.output.warn("%s: %s" % (self.patchname, logstr))
            else:
                self.output.info("%s: %s" % (self.patchname, logstr))

    patchlog = logging.getLogger("patch")
    if patchlog:
        patchlog.handlers = []
        patchlog.addHandler(PatchLogHandler())

    if not patch_file and not patch_string:
        return
    if patch_file:
        patchset = fromfile(patch_file)
    else:
        patchset = fromstring(patch_string.encode())

    if not patchset:
        raise ConanException("Failed to parse patch: %s" % (patch_file if patch_file else "string"))

    if not patchset.apply(root=base_path, strip=strip):
        raise ConanException("Failed to apply patch: %s" % patch_file)


# DETECT OS, VERSION AND DISTRIBUTIONS

class OSInfo(object):
    ''' Usage:
        print(os_info.is_linux) # True/False
        print(os_info.is_windows) # True/False
        print(os_info.is_macos) # True/False
        print(os_info.is_freebsd) # True/False
        print(os_info.is_solaris) # True/False

        print(os_info.linux_distro)  # debian, ubuntu, fedora, centos...

        print(os_info.os_version) # 5.1
        print(os_info.os_version_name) # Windows 7, El Capitan

        if os_info.os_version > "10.1":
            pass
        if os_info.os_version == "10.1.0":
            pass
    '''

    def __init__(self):
        self.os_version = None
        self.os_version_name = None
        self.is_linux = platform.system() == "Linux"
        self.linux_distro = None
        self.is_windows = platform.system() == "Windows"
        self.is_macos = platform.system() == "Darwin"
        self.is_freebsd = platform.system() == "FreeBSD"
        self.is_solaris = platform.system() == "SunOS"

        if self.is_linux:
            import distro
            self.linux_distro = distro.id()
            self.os_version = Version(distro.version())
            version_name = distro.codename()
            self.os_version_name = version_name if version_name != "n/a" else ""
            if not self.os_version_name and self.linux_distro == "debian":
                self.os_version_name = self.get_debian_version_name(self.os_version)
        elif self.is_windows:
            self.os_version = self.get_win_os_version()
            self.os_version_name = self.get_win_version_name(self.os_version)
        elif self.is_macos:
            self.os_version = Version(platform.mac_ver()[0])
            self.os_version_name = self.get_osx_version_name(self.os_version)
        elif self.is_freebsd:
            self.os_version = self.get_freebsd_version()
            self.os_version_name = "FreeBSD %s" % self.os_version
        elif self.is_solaris:
            self.os_version = Version(platform.release())
            self.os_version_name = self.get_solaris_version_name(self.os_version)

    @property
    def with_apt(self):
        return self.is_linux and self.linux_distro in \
            ("debian", "ubuntu", "knoppix", "linuxmint", "raspbian")

    @property
    def with_yum(self):
        return self.is_linux and self.linux_distro in \
            ("centos", "redhat", "fedora", "pidora", "scientific",
             "xenserver", "amazon", "oracle")

    def get_win_os_version(self):
        """
        Get's the OS major and minor versions.  Returns a tuple of
        (OS_MAJOR, OS_MINOR).
        """
        import ctypes

        class _OSVERSIONINFOEXW(ctypes.Structure):
            _fields_ = [('dwOSVersionInfoSize', ctypes.c_ulong),
                        ('dwMajorVersion', ctypes.c_ulong),
                        ('dwMinorVersion', ctypes.c_ulong),
                        ('dwBuildNumber', ctypes.c_ulong),
                        ('dwPlatformId', ctypes.c_ulong),
                        ('szCSDVersion', ctypes.c_wchar*128),
                        ('wServicePackMajor', ctypes.c_ushort),
                        ('wServicePackMinor', ctypes.c_ushort),
                        ('wSuiteMask', ctypes.c_ushort),
                        ('wProductType', ctypes.c_byte),
                        ('wReserved', ctypes.c_byte)]

        os_version = _OSVERSIONINFOEXW()
        os_version.dwOSVersionInfoSize = ctypes.sizeof(os_version)
        retcode = ctypes.windll.Ntdll.RtlGetVersion(ctypes.byref(os_version))
        if retcode != 0:
            return None

        return Version("%d.%d" % (os_version.dwMajorVersion, os_version.dwMinorVersion))

    def get_debian_version_name(self, version):
        if not version:
            return None
        elif version.major() == "8.Y.Z":
            return "jessie"
        elif version.major() == "7.Y.Z":
            return "wheezy"
        elif version.major() == "6.Y.Z":
            return "squeeze"
        elif version.major() == "5.Y.Z":
            return "lenny"
        elif version.major() == "4.Y.Z":
            return "etch"
        elif version.minor() == "3.1.Z":
            return "sarge"
        elif version.minor() == "3.0.Z":
            return "woody"

    def get_win_version_name(self, version):
        if not version:
            return None
        elif version.major() == "5.Y.Z":
            return "Windows XP"
        elif version.minor() == "6.0.Z":
            return "Windows Vista"
        elif version.minor() == "6.1.Z":
            return "Windows 7"
        elif version.minor() == "6.2.Z":
            return "Windows 8"
        elif version.minor() == "6.3.Z":
            return "Windows 8.1"
        elif version.minor() == "10.0.Z":
            return "Windows 10"

    def get_osx_version_name(self, version):
        if not version:
            return None
        elif version.minor() == "10.12.Z":
            return "Sierra"
        elif version.minor() == "10.11.Z":
            return "El Capitan"
        elif version.minor() == "10.10.Z":
            return "Yosemite"
        elif version.minor() == "10.9.Z":
            return "Mavericks"
        elif version.minor() == "10.8.Z":
            return "Mountain Lion"
        elif version.minor() == "10.7.Z":
            return "Lion"
        elif version.minor() == "10.6.Z":
            return "Snow Leopard"
        elif version.minor() == "10.5.Z":
            return "Leopard"
        elif version.minor() == "10.4.Z":
            return "Tiger"
        elif version.minor() == "10.3.Z":
            return "Panther"
        elif version.minor() == "10.2.Z":
            return "Jaguar"
        elif version.minor() == "10.1.Z":
            return "Puma"
        elif version.minor() == "10.0.Z":
            return "Cheetha"

    def get_freebsd_version(self):
        return platform.release().split("-")[0]

    def get_solaris_version_name(self, version):
        if not version:
            return None
        elif version.minor() == "5.10":
            return "Solaris 10"
        elif version.minor() == "5.11":
            return "Solaris 11"

try:
    os_info = OSInfo()
except Exception as exc:
    logger.error(exc)
    print("Error detecting os_info")


class SystemPackageTool(object):

    def __init__(self, runner=None, os_info=None, tool=None):
        env_sudo = os.environ.get("CONAN_SYSREQUIRES_SUDO", None)
        self._sudo = (env_sudo != "False" and env_sudo != "0")
        os_info = os_info or OSInfo()
        self._is_up_to_date = False
        self._tool = tool or self._create_tool(os_info)
        self._tool._sudo_str = "sudo " if self._sudo else ""
        self._tool._runner = runner or ConanRunner()

    def _create_tool(self, os_info):
        if os_info.with_apt:
            return AptTool()
        elif os_info.with_yum:
            return YumTool()
        elif os_info.is_macos:
            return BrewTool()
        else:
            return NullTool()

    def update(self):
        """
            Get the system package tool update command
        """
        self._is_up_to_date = True
        self._tool.update()

    def install(self, packages, update=True, force=False):
        '''
            Get the system package tool install command.
        '''
        packages = [packages] if isinstance(packages, str) else list(packages)
        if not force and self._installed(packages):
            return
        if update and not self._is_up_to_date:
            self.update()
        self._install_any(packages)

    def _installed(self, packages):
        for pkg in packages:
            if self._tool.installed(pkg):
                print("Package already installed: %s" % pkg)
                return True
        return False

    def _install_any(self, packages):
        if len(packages) == 1:
            return self._tool.install(packages[0])
        for pkg in packages:
            try:
                return self._tool.install(pkg)
            except ConanException:
                pass
        raise ConanException("Could not install any of %s" % packages)


class NullTool(object):
    def update(self):
        pass

    def install(self, package_name):
        print("Warn: Only available for linux with apt-get or yum or OSx with brew")

    def installed(self, package_name):
        return False


class AptTool(object):
    def update(self):
        _run(self._runner, "%sapt-get update" % self._sudo_str)

    def install(self, package_name):
        _run(self._runner, "%sapt-get install -y %s" % (self._sudo_str, package_name))

    def installed(self, package_name):
        exit_code = self._runner("dpkg -s %s" % package_name, None)
        return exit_code == 0


class YumTool(object):
    def update(self):
        _run(self._runner, "%syum check-update" % self._sudo_str)

    def install(self, package_name):
        _run(self._runner, "%syum install -y %s" % (self._sudo_str, package_name))

    def installed(self, package_name):
        exit_code = self._runner("rpm -q %s" % package_name, None)
        return exit_code == 0


class BrewTool(object):
    def update(self):
        _run(self._runner, "brew update")

    def install(self, package_name):
        _run(self._runner, "brew install %s" % package_name)

    def installed(self, package_name):
        exit_code = self._runner('test -n "$(brew ls --versions %s)"' % package_name, None)
        return exit_code == 0



def _run(runner, command):
    print("Running: %s" % command)
    if runner(command, True) != 0:
        raise ConanException("Command '%s' failed" % command)

