import json
import logging
import os
import shutil

from scale_build.exceptions import CallError
from scale_build.utils.git_utils import retrieve_git_remote_and_sha, retrieve_git_branch, update_git_manifest
from scale_build.utils.run import run
from scale_build.utils.variables import GIT_LOG_PATH, HASH_DIR, SOURCES_DIR

from .binary_package import BinaryPackage
from .utils import DEPENDS_SCRIPT_PATH, get_install_deps, normalize_build_depends, normalize_bin_packages_depends


logger = logging.getLogger(__name__)


class Package:
    def __init__(
        self, name, branch, repo, prebuildcmd=None, kernel_module=False, explicit_deps=None,
        generate_version=False, predepscmd=None, deps_path=None, subdir=None, deoptions=None, jobs=None,
    ):
        self.name = name
        self.branch = branch
        self.origin = repo
        self.prebuildcmd = prebuildcmd
        self.kernel_module = kernel_module
        self.explicit_deps = set(explicit_deps or set())
        self.generate_version = generate_version
        self.predepscmd = predepscmd
        self.deps_path = deps_path
        self.subdir = subdir
        self.deoptions = deoptions
        self.jobs = jobs
        self.initialized_deps = False
        self._binary_packages = []
        self.build_depends = set()
        self.source_package = None
        self.parent_changed = False
        self._build_time_dependencies = set()

    @property
    def package_path(self):
        pkg_path = self.source_path
        if self.subdir:
            pkg_path = os.path.join(pkg_path, self.subdir)
        return pkg_path

    @property
    def debian_control_file_path(self):
        if self.deps_path:
            return os.path.join(self.package_path, self.deps_path, 'control')
        else:
            return os.path.join(self.package_path, 'debian/control')

    @property
    def source_path(self):
        return os.path.join(SOURCES_DIR, self.name)

    @property
    def binary_packages(self):
        if self._binary_packages:
            return self._binary_packages

        if self.name == 'kernel' or (self.predepscmd and not self.deps_path):
            # We cannot determine dependency of this package because it does not probably have a control file
            # in it's current state - the only example we have is grub right now. Let's improve this if there are
            # more examples
            self._binary_packages.append(BinaryPackage(self.name, self.build_depends, self.name, self.name, set()))
            return self._binary_packages

        cp = run([DEPENDS_SCRIPT_PATH, self.debian_control_file_path])
        info = json.loads(cp.stdout)
        default_dependencies = {'kernel'} if self.kernel_module else set()
        self.build_depends = set(
            normalize_build_depends(info['source_package']['build_depends'])
        ) | default_dependencies
        self.source_package = info['source_package']['name']
        for bin_package in info['binary_packages']:
            self._binary_packages.append(BinaryPackage(
                bin_package['name'], self.build_depends, self.source_package, self.name,
                set(normalize_bin_packages_depends(bin_package['depends'] or ''))
            ))
            if self.name == 'truenas':
                self._binary_packages[-1].build_dependencies |= self._binary_packages[-1].install_dependencies

        return self._binary_packages

    def build_time_dependencies(self, all_binary_packages=None):
        if self._build_time_dependencies:
            return self._build_time_dependencies
        elif not all_binary_packages:
            raise CallError('Binary packages must be specified when computing build time dependencies')

        self._build_time_dependencies = get_install_deps(
            all_binary_packages, set(), self.build_depends
        ) | self.explicit_deps
        return self._build_time_dependencies

    @property
    def chroot_source_directory(self):
        return 'dpkg-src'

    @property
    def hash_changed(self):
        if self.name == 'truenas':
            # truenas is special and we want to rebuild it always
            # TODO: Do see why that is so
            return True

        source_hash = run(['git', '-C', self.source_path, 'rev-parse', '--verify', 'HEAD']).stdout.decode().strip()
        existing_hash = None
        if os.path.exists(self.hash_path):
            with open(self.hash_path, 'r') as f:
                existing_hash = f.read().strip()
        if source_hash == existing_hash:
            return run(
                ['git', '-C', self.source_path, 'diff-files', '--quiet', '--ignore-submodules'], check=False
            ).returncode != 0
        else:
            return True

    @property
    def rebuild(self):
        return self.hash_changed or self.parent_changed

    @property
    def hash_path(self):
        return os.path.join(HASH_DIR, f'{self.name}.hash')

    @property
    def exists(self):
        return os.path.exists(self.source_path)

    def retrieve_current_remote_origin_and_sha(self):
        if self.exists:
            return retrieve_git_remote_and_sha(self.source_path)
        else:
            return {'url': None, 'sha': None}

    def update_git_manifest(self):
        info = self.retrieve_current_remote_origin_and_sha()
        update_git_manifest(info['url'], info['sha'])

    def checkout(self, branch_override=None):
        origin_url = self.retrieve_current_remote_origin_and_sha()['url']
        branch = branch_override or self.branch
        if branch == self.existing_branch and self.origin == origin_url:
            logger.debug(f'Updating git repo [{self.name}] ({GIT_LOG_PATH})')
            with open(GIT_LOG_PATH, 'w') as f:
                run(['git', '-C', self.source_path, 'fetch', '--unshallow'], stdout=f, stderr=f, check=False)
                run(['git', '-C', self.source_path, 'fetch', 'origin', branch], stdout=f, stderr=f)
                run(['git', '-C', self.source_path, 'reset', '--hard', f'origin/{branch}'], stdout=f, stderr=f)
        else:
            logger.debug(f'Checking out git repo [{self.name}] ({GIT_LOG_PATH})')
            shutil.rmtree(self.source_path, ignore_errors=True)
            with open(GIT_LOG_PATH, 'w') as f:
                run(['git', 'clone', '--depth=1', '-b', branch, self.origin, self.source_path], stdout=f, stderr=f)

        self.update_git_manifest()

    @property
    def deflags(self):
        if self.name == 'kernel':
            return [f'-j{os.cpu_count()}', '-us', '-uc', '-b']
        else:
            return [f'-j{self.jobs if self.jobs else os.cpu_count()}', '-us', '-uc', '-b']

    @property
    def existing_branch(self):
        if not self.exists:
            return None
        return retrieve_git_branch(self.source_path)