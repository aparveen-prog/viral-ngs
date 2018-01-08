'''Automated recording of data provenance and metrics.  This lets you answer questions like: how was a given data file made?  Which workflow
versions tend to produce the best results, according to given metrics?

This module deals with data recording; ../data_utils.py deals with data querying.

The unit of recording in this module is one command (see cmd.py), as opposed to one Python function or one whole workflow.
To use this module, import InFile and OutFile from it; then, when defining argparse arguments for a command, in add_argument()
use "type=InFile" for input files or "type=OutFile" for output files. Until provenance tracking is enabled (see below how),
InFile and OutFile are defined to be simply str.  To enable provenance tracking, set the environment variable
VIRAL_NGS_METADATA_PATH to a writable directory.  Then, whenever a command is run, a file is written to that directory
recording the command, all its parameters, the input and output files, and details of the run environment.
Input and output files are identified by a hash of their contents, so that copied/moved/renamed files can be properly identified.
For each file, we also record metadata such as name, size, and modification date.


: for each data file, automatically keeping track of how it was created -- from which inputs, by which 
code version and with what parameters.


Note that only files actually used are listed in the metadata; optional files not specified in a given command invocation are not.


See data_utils.py for utils that create useful reports from provenance data.

Environment variables used:

   VIRAL_NGS_METADATA_PATH: location to which metadata should be recorded.  If this environment variable is not set, metadata recording
       is disabled, and this module has no effect.

Implementation notes:

Metadata recording is done on a best-effort basis.  If metadata recording fails for any reason, a warning is logged, but no error is raised.
'''

__author__ = "ilya@broadinstitute.org"
__all__ = ["InFile", "OutFile", "add_metadata_tracking", "is_metadata_tracking_enabled", "metadata_dir", "load_provenance_graph"]

# built-ins
import argparse
import logging
import os
import os.path
import sys
import stat
import platform
import shutil
import sys
import time
import uuid
import socket
import getpass
import pwd
import json
import traceback
import inspect
import collections
import functools
import itertools
import operator

# intra-module
import util.file
import util.misc
import util.version


_log = logging.getLogger(__name__)

#########################
# Recording of metadata #
#########################

VIRAL_NGS_METADATA_FORMAT='1.0.0'

def metadata_dir():
    """Returns the directory to which metadata was recorded, as specified by the environment variable VIRAL_NGS_METADATA_PATH.
    Raises an error if the environment variable is not defined.
    """
    return os.environ['VIRAL_NGS_METADATA_PATH']

def is_metadata_tracking_enabled():
    return 'VIRAL_NGS_METADATA_PATH' in os.environ

class FileArg(str):

    '''Abstract base class for argparse parameter type for input and output files, 
    which provides error-checking and optional provenance tracking.'''
    
    def __new__(cls, *args, **kw):
        return str.__new__(cls, *args, **kw)

class InFile(FileArg):

    '''Argparse parameter type for input files'''

    def __new__(cls, *args, **kw):

        # check that the file is readable, force an immediate error if it is not
        with open(args[0]):
            pass

        return FileArg.__new__(cls, *args, **kw)

class OutFile(FileArg):

    '''Argparse parameter type for output files'''

    def __new__(cls, *args, **kw):
        if 'VIRAL_NGS_SKIP_CMD' not in os.environ:
            fname = args[0]
            if not os.path.exists(fname):
                with open(fname, 'w'):
                    pass
                os.unlink(fname)
            else:
                if not (os.path.isfile(fname) and os.access(fname, os.W_OK)):
                    raise IOError('Output filel not writable: ' + fname)

        return FileArg.__new__(cls, *args, **kw)

if not is_metadata_tracking_enabled():
    InFile, OutFile = str, str

class Hasher(object):
    """Manages computation of file hashes.

    It might also cache the actual contents of files, depending on size.
    """

    def __init__(self, hash_algorithm='sha1'):
        self.hash_algorithm = hash_algorithm

    def __call__(self, file):
        file_hash = ''
        try:
            if os.path.isfile(file) and not stat.S_ISFIFO(os.stat(file).st_mode):
                file_hash = self.hash_algorithm + '_' + util.file.hash_file(file, hash_algorithm=self.hash_algorithm)
        except Exception:
            _log.warning('Cannot compute hash for {}: {}'.format(file, traceback.format_exc()))
        return file_hash


def create_run_id(t=None):
    """Generate a unique ID for a run (set of steps run as part of one workflow)."""
    return util.file.string_to_file_name('__'.join(map(str, (time.strftime('%Y%m%d%H%M%S', time.localtime(t))[2:], getpass.getuser(),
                                                             os.path.basename(os.getcwd()), uuid.uuid4()))))[:210]

def set_run_id():
    """Generate and record in the environment a unique ID for a run (set of steps run as part of one workflow)."""
    os.environ['VIRAL_NGS_METADATA_RUN_ID'] = create_run_id()

def tag_code_version(tag, push_to=None):
    """Create a lightweight git tag for the current state of the project repository, even if the state is dirty.
    If the repository is dirty, use the 'git stash create' command to create a commit representing the current state,
    and tag that; else, tag the existing clean state.  If `push_to` is not None, push the tag to the specified git remote.
    Return the git hash for the created git tag.  In case of any error, print a warning and return an empty string.
    """

    def run_cmd(cmd):
        """Run a command and return its output"""
        out = ''
        result = util.misc.run_and_print(cmd.strip().split(), silent=True)
        if result.returncode == 0:
            out = result.stdout
            if not isinstance(out, str):
                out = out.decode('utf-8')
            out = out.strip()
        return out

    code_hash = ''

    try:
        with util.file.pushd_popd(util.version.get_project_path()):
            code_hash = run_cmd('git stash create') or run_cmd('git log -1 --format="%H"')
            run_cmd('git tag ' + tag + ' ' + code_hash)
            if push_to:
                run_cmd('git push ' + push_to + ' ' + tag)
    except Exception:
        _log.warning('Could not create git tag: {}'.format(traceback.format_exc()))

    return code_hash

def add_metadata_tracking(cmd_parser, cmd_main, cmd_main_orig):
    """Add provenance tracking to the given command.  

    Called from util.cmd.attach_main().
    
    Args:
        cmd_parser: parser for a command defined in a script
        cmd_main: function implementing the command. Function takes one parameter, an argparse.Namespace, giving the values of the command's
             arguments.
        cmd_main_orig: the original cmd_main function defined in the command script, if cmd_main is a wrapper around the original 
             function, else same as cmd_main.

    Returns:
        a wrapper for cmd_main, which has the same signature but adds metadata recording if enabled.
    """

    cmd_parser.add_argument('--metadata', action='append', nargs=2, metavar=('ATTRIBUTE', 'VALUE'),
                            help='attach metadata to this step (step=this specific execution of this command)')

    cmd_module=os.path.splitext(os.path.basename(inspect.getfile(cmd_main_orig)))[0]
    cmd_name=cmd_main_orig.__name__

    def _run_cmd_with_tracking(args):

        args_dict = vars(args).copy()
        delattr(args, 'metadata')

        cmd_exception, cmd_exception_str, cmd_result = (None,)*3

        try:
            _log.info('calling command {}.{}; metadata tracking: {}'.format(cmd_module, cmd_name, is_metadata_tracking_enabled()))
            beg_time = time.time()

            # *** Run the actual command ***
            cmd_result = cmd_main(args) if 'VIRAL_NGS_SKIP_CMD' not in os.environ else None
        except Exception as e:
            cmd_exception = e
            cmd_exception_str = traceback.format_exc()
        finally:
            try:  # if any errors happen during metadata recording just issue a warning
                if is_metadata_tracking_enabled():
                    end_time = time.time()
                    _log.info('recording metadata to {}'.format(metadata_dir()))

                    #
                    # Record data pertaining to the whole step
                    # 

                    # run_id is the same for all steps run as part of a single workflow.
                    # if not given in the environment, create a run_id for a one-step workflow consisting of just this step.
                    run_id = os.environ.get('VIRAL_NGS_METADATA_RUN_ID', create_run_id(beg_time))
                    step_id = '__'.join(map(str, (create_run_id(beg_time), cmd_module, cmd_name)))

                    # record the code version used to run this step
                    code_repo = os.path.join(metadata_dir(), 'code_repo')
                    code_hash = tag_code_version('cmd_' + step_id, push_to=code_repo if os.path.isdir(code_repo) else None)

                    step_data = dict(format=VIRAL_NGS_METADATA_FORMAT)

                    metadata_from_cmd_line = args_dict.pop('metadata', {}) or {}
                    metadata_from_cmd_return = cmd_result if isinstance(cmd_result, collections.Mapping) else {}

                    args_dict.pop('func_main', '')

                    step_data['step'] = dict(step_id=step_id, run_id=run_id,
                                             cmd_module=cmd_module, cmd_name=cmd_name,
                                             version_info=dict(viral_ngs_version=util.version.get_version(),
                                                               viral_ngs_path=util.version.get_project_path(),
                                                               viral_ngs_path_real=os.path.realpath(util.version.get_project_path()),
                                                               code_hash=code_hash),
                                             run_env=dict(metadata_dir=metadata_dir(),
                                                          platform=platform.platform(), 
                                                          cpus=util.misc.available_cpu_count(), host=socket.getfqdn(),
                                                          user=getpass.getuser(),
                                                          cwd=os.getcwd()),
                                             run_info=dict(beg_time=beg_time, end_time=end_time, duration=end_time-beg_time,
                                                           exception=cmd_exception_str,
                                                           argv=tuple(sys.argv),
                                                           cmd_was_skipped = 'VIRAL_NGS_SKIP_CMD' in os.environ),
                                             args=args_dict,
                                             metadata_from_cmd_line=metadata_from_cmd_line,
                                             metadata_from_cmd_return=metadata_from_cmd_return)
                    
                    #
                    # Record information about the input and output files used by the command
                    #

                    hasher = Hasher()

                    step_data.update(in_files={}, out_files={})

                    # Input and output files are specified by argparse arguments of type InFile and OutFile.
                    # Additionally, if the command reads and writes files that were not passed as arguments 
                    # (e.g. if a filename gets constructed based on the arguments), the command can tell us about these files
                    # by returning metadata values of type InFile/OutFile.
                    for arg, val in itertools.chain(args_dict.items(), metadata_from_cmd_return.items()):

                        def get_FileArgs(val, idx_prefix=()):
                            """Extract any file arguments from a command parameter or metadata value.

                            Args:
                                val: the value of a command parameter or metadata; can be a single value, a list of values 
                                  (for an argparse argument with with nargs=N), or a list of lists (with nargs=N and action='append').
                                idx_prefix: tuple to prepend to all returned indices (see return value below)
                            Returns:
                                a flat list of pairs (idx, file_arg), one per InFile/OutFile value contained in `val`, where file_arg
                                is the InFile/OutFile value, and idx is the index of the file within the original argument value
                                ( () if the value was just a file, (i,) if the value was a list of files, (i,j) if it was a list of
                                  lists of files ).  Note that if the `val` argument was not a FileArg or list of FileArgs or
                                  list of lists of FileArgs, the return value is an empty list.
                            """
                            return \
                                val and isinstance(val, FileArg) and [(idx_prefix, val)] or \
                                isinstance(val, list) and functools.reduce(operator.concat,
                                                                           [get_FileArgs(v, idx_prefix+(i,)) 
                                                                            for i, v in enumerate(val)], []) or \
                                []

                        for idx, file_arg in get_FileArgs(val):
                            file_info = dict(fname=file_arg, realpath=os.path.realpath(file_arg), abspath=os.path.abspath(file_arg),
                                             arg=arg, arg_idx=idx)

                            if isinstance(file_arg, InFile) or cmd_exception is None:
                               
                                file_info.update(hash=hasher(file_arg))

                                try:
                                    file_stat = os.stat(file_arg)
                                    file_info.update(size=file_stat[stat.ST_SIZE],
                                                     mtime=file_stat[stat.ST_MTIME], ctime=file_stat[stat.ST_CTIME])
                                    file_info.update(owner=pwd.getpwuid(file_stat[stat.ST_UID]).pw_name)
                                except Exception:
                                    _log.warning('Error getting file info for {} ({})'.format(file_arg, traceback.format_exc()))
                                    
                            port_name = arg+''.join('_'+str(i) for i in idx)
                            step_data['in_files' if isinstance(file_arg, InFile) else 'out_files'][port_name] = file_info

                        # end: for idx, file_arg in get_FileArgs(val):

                    # end: for arg, val in itertools.chain(args_dict.items(), metadata.items())

                    util.file.dump_file(os.path.join(metadata_dir(), step_id+'.json'),
                                        json.dumps(step_data, sort_keys=True, indent=4, default=str))

            except Exception:
                # metadata recording is not an essential operation, so if anything goes wrong we just print a warning
                _log.warning('Error recording metadata ({})'.format(traceback.format_exc()))

        if cmd_exception:
            raise cmd_exception

    return _run_cmd_with_tracking

########################
# Analysis of metadata #
########################

class ProvenanceGraph(object):

    '''Provenance graph representing a set of computations.  It has two types of nodes: steps and files.  A step node represents
    a computation step (a specific execution of a specific command); a file node represents a data file.  Edges go from input files
    of a step to the step, and from a step to its output files.  Various information about the parameters and execution of a step
    is represented as attributes of the step node.'''

    def __init__(self):
        self.pgraph = networkx.DiGraph()

    def load(path=None):
        """Read the provenance graph."""
        
        if path is None: path = metadata_dir()

        for step_record_fname in os.listdir(path):
            _log.info('loading step {}'.format(step_record_fname))
            if step_record_fname.endswith('.json'):
                step_record = json.loads(util.file.slurp_file(os.path.join(path, step_record_fname)))
                step_id = step_record['step']['step_id']
                pgraph.add_node(step_id, node_kind='step', **step_record['step'])
                for file_kind in ('in_files', 'out_files'):
                    for port_name, file_info in step_record[file_kind].items():
                        port_id = step_id + '__' + port_name
                        pgraph.add_node(port_id, **file_info)
                        pgraph.add_node(port_id, node_kind=('input' if file_kind=='in_files' else 'output'))
                        pgraph.add_edge(*((port_id, step_id) if file_kind=='in_files' else (step_id, port_id)))
                        if 'hash' in file_info:
                            file_id = file_info['hash']
                            if file_id:
                                pgraph.add_node(file_id, node_kind='file')
                                pgraph.add_edge(*((file_id, port_id) if file_kind=='in_files' else (port_id, file_id)))

        return pgraph


def compute_paths(paths_file):
    """Compute computation paths, and their attributes."""

    g = load_provenance_graph()

    #start_nodes = [ n for n in pgraph if dict().viewitems() <= g.nodes[n].viewitems() ]


if __name__ == '__main__':
    print(InFile('hi.txt')=='hi.txt', hash(InFile('hi.txt'))==hash('hi.txt'))
