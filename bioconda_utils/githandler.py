"""Abstraction layer handling git actions"""

import asyncio
import logging
import os
import tempfile
from typing import List

import git


logger = logging.getLogger(__name__)  # pylint: disable=invalid-name


class GitHandlerFailure(Exception):
    """Something went wrong interacting with git"""


class GitHandlerBase():
    """GitPython abstraction

    We have to work with three git repositories, the local checkout,
    the project primary repository and a working repository. The
    latter may be a fork or may be the same as the primary.


    Arguments:
      repo: GitPython Repo object (created by subclasses)
      dry_run: Don't push anything to remote
      home: string occurring in remote url marking primary project repo
      fork: string occurring in remote url marking forked repo
    """
    def __init__(self, repo: git.Repo,
                 dry_run: bool,
                 home='bioconda/bioconda-recipes',
                 fork=None) -> None:
        #: GitPython Repo object representing our repository
        self.repo: git.Repo = repo
        if self.repo.is_dirty():
            raise RuntimeError("Repository is in dirty state. Bailing out")
        #: Dry-Run mode - don't push or commit anything
        self.dry_run = dry_run
        #: Remote pointing to primary project repo
        self.home_remote = self.get_remote(home)
        if fork is not None:
            #: Remote to pull from
            self.fork_remote = self.get_remote(fork)
        else:
            self.fork_remote = self.home_remote

        #: Semaphore for things that mess with workding directory
        self.lock_working_dir = asyncio.Semaphore(1)


    def close(self):
        """Release resources allocated"""
        self.repo.close()

    def get_remote(self, desc: str):
        """Finds first remote containing **desc** in one of its URLs"""
        if desc in [r.name for r in self.repo.remotes]:
            return self.repo.remotes[desc]
        remotes = [r for r in self.repo.remotes
                   if any(filter(lambda x: desc in x, r.urls))]
        if not remotes:
            raise KeyError(f"No remote matching '{desc}' found")
        return remotes[0]

    async def branch_is_current(self, branch, path: str, master="master") -> bool:
        """Checks if **branch** has the most recent commit modifying **path**
        as compared to **master**"""
        proc = await asyncio.create_subprocess_exec(
            'git', 'log', '-1', '--oneline', '--decorate',
            f'{master}...{branch.name}', '--', path,
            stdout=asyncio.subprocess.PIPE)
        stdout, _ = await proc.communicate()
        return branch.name in stdout.decode('ascii')

    def delete_local_branch(self, branch) -> None:
        """Deletes **branch** locally"""
        git.Reference.delete(self.repo, branch)

    def delete_remote_branch(self, branch_name: str) -> None:
        """Deletes **branch** on fork remote"""
        if not self.dry_run:
            logger.info("Deleting branch %s", branch_name)
            self.fork_remote.push(":" + branch_name)
        else:
            logger.info("Would delete branch %s", branch_name)

    def get_local_branch(self, branch_name: str):
        """Finds local branch named **branch_name**"""
        if branch_name in self.repo.branches:
            return self.repo.branches[branch_name]
        return None

    def get_remote_branch(self, branch_name: str):
        """Finds fork remote branch named **branch_name**"""
        if branch_name in self.fork_remote.refs:
            return self.fork_remote.refs[branch_name]
        logger.error("Branch %s not found!", branch_name)
        logger.info("  Have branches: %s", self.fork_remote.refs)
        return None

    def read_from_branch(self, branch, file_name: str) -> str:
        """Reads contents of file **file_name** from git branch **branch**"""
        abs_file_name = os.path.abspath(file_name)
        abs_repo_root = os.path.abspath(self.repo.working_dir)
        if not abs_file_name.startswith(abs_repo_root):
            raise RuntimeError(
                f"File {abs_file_name} not inside {abs_repo_root}"
            )
        rel_file_name = abs_file_name[len(abs_repo_root):].lstrip("/")
        return (branch.commit.tree / rel_file_name).data_stream.read().decode("utf-8")

    def create_local_branch(self, branch_name: str):
        """Creates local branch from remote **branch_name**"""
        remote_branch = self.get_remote_branch(branch_name)
        self.repo.create_head(branch_name, remote_branch.commit)
        return self.get_local_branch(branch_name)

    def prepare_branch(self, branch_name: str) -> None:
        """Checks out **branch_name**, creating it from home remote master if needed"""
        if branch_name not in self.repo.heads:
            logger.info("Creating new branch %s", branch_name)
            from_commit = self.home_remote.fetch('master')[0].commit
            self.repo.create_head(branch_name, from_commit)
        logger.info("Checking out branch %s", branch_name)
        branch = self.repo.heads[branch_name]
        branch.checkout()

    def commit_and_push_changes(self, files: List[str], branch_name: str,
                                msg: str, sign=False) -> bool:
        """Create recipe commit and pushes to upstream remote

        Returns:
          Boolean indicating whether there were changes committed
        """
        self.repo.index.add(files)
        if not self.repo.index.diff("HEAD"):
            return False
        if sign:
            # Gitpyhon does not support signing, so we use the command line client here
            self.repo.index.write()
            self.repo.git.commit('-S', '-m', msg)
        else:
            self.repo.index.commit(msg)
        if not self.dry_run:
            logger.info("Pushing branch %s", branch_name)
            res = self.fork_remote.push(branch_name)
            failed = res[0].flags & ~(git.PushInfo.FAST_FORWARD | git.PushInfo.NEW_HEAD)
            if failed:
                logger.error("Failed to push branch %s: %s", branch_name, res[0].summary)
                raise GitHandlerFailure(res[0].summary)
        else:
            logger.info("Would push branch %s", branch_name)
        return True

    def set_user(self, user: str, email: str = None, key: str = None) -> None:
        with self.repo.config_writer() as writer:
            writer.set_value("user", "name", user)
            email = email or f"{user}@users.noreply.github.com"
            writer.set_value("user", "email", email)
            if key is not None:
                writer.set_value("user", "signingkey", key)


class GitHandler(GitHandlerBase):
    """GitHandler for working with a pre-existing local checkout of bioconda-recipes

    Restores the branch active when created upon calling `close()`.
    """
    def __init__(self, folder: str,
                 dry_run=False,
                 home='bioconda/bioconda-recipes',
                 fork=None) -> None:
        repo = git.Repo(folder, search_parent_directories=True)
        super().__init__(repo, dry_run, home, fork)

        #: Branch to restore after running
        self.prev_active_branch = self.repo.active_branch

        ## Update the local repo
        logger.warning("Checking out master")
        self.get_local_branch("master").checkout()
        logger.info("Updating master to latest project master")
        self.home_remote.pull("master")
        logger.info("Updating and pruning remotes")
        self.home_remote.fetch(prune=True)
        self.fork_remote.fetch(prune=True)

    def close(self) -> None:
        """Release resources allocated"""
        logger.warning("Switching back to %s", self.prev_active_branch.name)
        self.prev_active_branch.checkout()
        super().close()

class TempGitHandler(GitHandlerBase):
    """GitHandler for working with temporary working directories created on the fly
    """
    def __init__(self,
                 username: str = None,
                 password: str = None,
                 url_format="https://{userpass}github.com/{path}.git",
                 home="bioconda/bioconda-recipes",
                 fork=None, dry_run=False) -> None:
        userpass = ""
        safe_userpass = ""
        if password is not None and username is None:
            username = "x-access-token"
        if username is not None:
            safe_userpass = userpass = username
            if password is not None:
                userpass += ":" + password
                safe_userpass += ":XXXXXX"
            userpass += "@"
            safe_userpass += "@"

        self.tempdir = tempfile.TemporaryDirectory()

        home_url = url_format.format(userpass=userpass, path=home)
        safe_home_url = url_format.format(userpass=safe_userpass, path=home)
        logger.warning("Cloning %s to %s", safe_home_url, self.tempdir.name)
        repo = git.Repo.clone_from(home_url, self.tempdir.name, depth=1)

        if fork is not None:
            fork_url = url_format.format(userpass=userpass, path=fork)
            safe_fork_url = url_format.format(userpass=safe_userpass, path=fork)
            logger.warning("Adding remote fork %s", safe_fork_url)
            fork_remote = repo.create_remote("fork", fork_url)
            fork_remote.fetch()

        super().__init__(repo, dry_run, home, fork)

    def close(self) -> None:
        """Remove temporary clone and cleanup resources"""
        super().close()
        logger.warning("Removing repo from %s", self.tempdir.name)
        self.tempdir.cleanup()
