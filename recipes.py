#!/usr/bin/env python
# Copyright 2015 The LUCI Authors. All rights reserved.
# Use of this source code is governed under the Apache License, Version 2.0
# that can be found in the LICENSE file.

"""Tool to interact with recipe repositories.

This tool operates on the nearest ancestor directory containing an
infra/config/recipes.cfg.
"""

import argparse
import collections
import json
import logging
import os
import subprocess
import sys

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT_DIR)

from recipe_engine import env
from recipe_engine import arguments_pb2
from google.protobuf import json_format as jsonpb

def get_package_config(args):
  from recipe_engine import package

  assert args.package, 'No recipe config (--package) given.'
  assert os.path.exists(args.package), (
      'Given recipes config file %s does not exist.' % args.package)
  return (
      package.InfraRepoConfig().from_recipes_cfg(args.package),
      package.ProtoFile(args.package)
  )


def simulation_test(package_deps, args):
  try:
    from recipe_engine import simulation_test
  except ImportError:
    logging.error(
        'Error while importing testing libraries. You may be missing the pip'
        ' package "coverage". Install it, or use the --use-bootstrap command'
        ' line argument when calling into the recipe engine, which will install'
        ' it for you.')
    raise

  from recipe_engine import loader

  _, config_file = get_package_config(args)
  universe = loader.RecipeUniverse(package_deps, config_file)
  universe_view = loader.UniverseView(universe, package_deps.root_package)

  simulation_test.main(universe_view, args=json.loads(args.args))


def lint(package_deps, args):
  from recipe_engine import lint_test
  from recipe_engine import loader

  _, config_file = get_package_config(args)
  universe = loader.RecipeUniverse(package_deps, config_file)
  universe_view = loader.UniverseView(universe, package_deps.root_package)

  lint_test.main(universe_view, args.whitelist or [])


def handle_recipe_return(recipe_result, result_filename, stream_engine):
  if 'recipe_result' in recipe_result.result:
    result_string = json.dumps(
        recipe_result.result['recipe_result'], indent=2)
    if result_filename:
      with open(result_filename, 'w') as f:
        f.write(result_string)
    with stream_engine.make_step_stream('recipe result') as s:
      with s.new_log_stream('result') as l:
        l.write_split(result_string)

  if 'traceback' in recipe_result.result:
    with stream_engine.make_step_stream('Uncaught Exception') as s:
      with s.new_log_stream('exception') as l:
        for line in recipe_result.result['traceback']:
          l.write_line(line)

  if 'reason' in recipe_result.result:
    with stream_engine.make_step_stream('Failure reason') as s:
      with s.new_log_stream('reason') as l:
        for line in recipe_result.result['reason'].splitlines():
          l.write_line(line)

  if 'status_code' in recipe_result.result:
    return recipe_result.result['status_code']
  else:
    return 0


def run(package_deps, args, op_args):
  from recipe_engine import run as recipe_run
  from recipe_engine import loader
  from recipe_engine import step_runner
  from recipe_engine import stream
  from recipe_engine import stream_logdog

  def get_properties_from_args(args):
    properties = dict(x.split('=', 1) for x in args)
    for key, val in properties.iteritems():
      try:
        properties[key] = json.loads(val)
      except (ValueError, SyntaxError):
        pass  # If a value couldn't be evaluated, keep the string version
    return properties

  def get_properties_from_file(filename):
    properties_file = sys.stdin if filename == '-' else open(filename)
    properties = json.load(properties_file)
    if filename == '-':
      properties_file.close()
    assert isinstance(properties, dict)
    return properties

  def get_properties_from_json(props):
    return json.loads(props)

  def get_properties_from_operational_args(op_args):
    if not op_args.properties.property:
      return None
    return _op_properties_to_dict(op_args.properties.property)


  arg_properties = get_properties_from_args(args.props)
  op_properties = get_properties_from_operational_args(op_args)
  assert len(filter(bool,
      (arg_properties, args.properties_file, args.properties,
       op_properties))) <= 1, (
          'Only one source of properties is allowed')
  if args.properties:
    properties = get_properties_from_json(args.properties)
  elif args.properties_file:
    properties = get_properties_from_file(args.properties_file)
  elif op_properties is not None:
    properties = op_properties
  else:
    properties = arg_properties

  properties['recipe'] = args.recipe

  os.environ['PYTHONUNBUFFERED'] = '1'
  os.environ['PYTHONIOENCODING'] = 'UTF-8'

  _, config_file = get_package_config(args)
  universe_view = loader.UniverseView(
      loader.RecipeUniverse(
          package_deps, config_file), package_deps.root_package)

  workdir = (args.workdir or
      os.path.join(os.path.dirname(os.path.realpath(__file__)), 'workdir'))
  logging.info('Using %s as work directory' % workdir)
  if not os.path.exists(workdir):
    os.makedirs(workdir)

  old_cwd = os.getcwd()
  os.chdir(workdir)

  # Construct our stream engines. We may want to share stream events with more
  # than one StreamEngine implementation, so we will accumulate them in a
  # "stream_engines" list and compose them into a MultiStreamEngine.
  def build_annotation_stream_engine():
    return stream.AnnotatorStreamEngine(
        sys.stdout,
        emit_timestamps=(args.timestamps or
                         op_args.annotation_flags.emit_timestamp),
    )

  stream_engines = []
  if op_args.logdog.streamserver_uri:
    logging.debug('Using LogDog with parameters: [%s]', op_args.logdog)
    stream_engines.append(stream_logdog.StreamEngine(
        streamserver_uri=op_args.logdog.streamserver_uri,
        name_base=(op_args.logdog.name_base or None),
        dump_path=op_args.logdog.final_annotation_dump_path,
    ))

    # If we're teeing, also fold in a standard annotation stream engine.
    if op_args.logdog.tee:
      stream_engines.append(build_annotation_stream_engine())
  else:
    # Not using LogDog; use a standard annotation stream engine.
    stream_engines.append(build_annotation_stream_engine())
  multi_stream_engine = stream.MultiStreamEngine.create(*stream_engines)

  # Have a top-level set of invariants to enforce StreamEngine expectations.
  with stream.StreamEngineInvariants.wrap(multi_stream_engine) as stream_engine:
    # Emit initial properties if configured to do so.
    if op_args.annotation_flags.emit_initial_properties:
      with stream_engine.new_step_stream('Initial Properties') as s:
        for key in sorted(properties.iterkeys()):
          s.set_build_property(key, json.dumps(properties[key], sort_keys=True))

    try:
      ret = recipe_run.run_steps(
          properties, stream_engine,
          step_runner.SubprocessStepRunner(stream_engine),
          universe_view=universe_view)
    finally:
      os.chdir(old_cwd)

    return handle_recipe_return(ret, args.output_result_json, stream_engine)


def remote(args):
  from recipe_engine import remote

  return remote.main(args)


def autoroll(args):
  from recipe_engine import autoroll

  repo_root, config_file = get_package_config(args)

  return autoroll.main(args, repo_root, config_file)


class ProjectOverrideAction(argparse.Action):
  def __call__(self, parser, namespace, values, option_string=None):
    p = values.split('=', 2)
    if len(p) != 2:
      raise ValueError("Override must have the form: repo=path")
    project_id, path = p

    v = getattr(namespace, self.dest, None)
    if v is None:
      v = collections.OrderedDict()
      setattr(namespace, self.dest, v)

    if v.get(project_id):
      raise ValueError("An override is already defined for [%s] (%s)" % (
                       project_id, v[project_id]))
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isdir(path):
      raise ValueError("Override path [%s] is not a directory" % (path,))
    v[project_id] = path


def depgraph(package_deps, args):
  from recipe_engine import depgraph
  from recipe_engine import loader

  _, config_file = get_package_config(args)
  universe = loader.RecipeUniverse(package_deps, config_file)

  depgraph.main(universe, package_deps.root_package,
                args.ignore_package, args.output, args.recipe_filter)


def refs(package_deps, args):
  from recipe_engine import refs
  from recipe_engine import loader

  _, config_file = get_package_config(args)
  universe = loader.RecipeUniverse(package_deps, config_file)

  refs.main(universe, package_deps.root_package, args.modules, args.transitive)


def doc(package_deps, args):
  from recipe_engine import doc
  from recipe_engine import loader

  _, config_file = get_package_config(args)
  universe = loader.RecipeUniverse(package_deps, config_file)
  universe_view = loader.UniverseView(universe, package_deps.root_package)

  doc.main(universe_view)


def info(args):
  from recipe_engine import package
  repo_root, config_file = get_package_config(args)
  package_spec = package.PackageSpec.load_proto(config_file)

  if args.recipes_dir:
    print package_spec.recipes_path


# Map of arguments_pb2.Property "value" oneof conversion functions.
#
# The fields here should be kept in sync with the "value" oneof field names in
# the arguments_pb2.Arguments.Property protobuf message.
_OP_PROPERTY_CONV = {
    's': lambda prop: prop.s,
    'int': lambda prop: prop.int,
    'uint': lambda prop: prop.uint,
    'd': lambda prop: prop.d,
    'b': lambda prop: prop.b,
    'data': lambda prop: prop.data,
    'map': lambda prop: _op_properties_to_dict(prop.map.property),
    'list': lambda prop: [_op_property_value(v) for v in prop.list.property],
}

def _op_property_value(prop):
  """Returns the Python-converted value of an arguments_pb2.Property.

  Args:
    prop (arguments_pb2.Property): property to convert.
  Returns: The converted value.
  Raises:
    ValueError: If "prop" is incomplete or invalid.
  """
  typ = prop.WhichOneof('value')
  conv = _OP_PROPERTY_CONV.get(typ)
  if not conv:
    raise ValueError('Unknown property field [%s]' % (typ,))
  return conv(prop)


def _op_properties_to_dict(pmap):
  """Creates a properties dictionary from an arguments_pb2.PropertyMap entry.

  Args:
    pmap (arguments_pb2.PropertyMap): Map to convert to dictionary form.
  Returns (dict): A dictionary derived from the properties in "pmap".
  """
  return dict((k, _op_property_value(pmap[k])) for k in pmap)


def main():
  from recipe_engine import package

  # Super-annoyingly, we need to manually parse for simulation_test since
  # argparse is bonkers and doesn't allow us to forward --help to subcommands.
  # Save old_args for if we're using bootstrap
  original_sys_argv = sys.argv[:]
  if 'simulation_test' in sys.argv:
    index = sys.argv.index('simulation_test')
    sys.argv = sys.argv[:index+1] + [json.dumps(sys.argv[index+1:])]

  parser = argparse.ArgumentParser(
      description='Interact with the recipe system.')

  parser.add_argument(
      '--package',
      help='Path to recipes.cfg of the recipe package to operate on'
        ', usually in infra/config/recipes.cfg')
  parser.add_argument(
      '--deps-path',
      help='Path where recipe engine dependencies will be extracted.')
  parser.add_argument(
      '--verbose', '-v', action='count',
      help='Increase logging verboisty')
  # TODO(phajdan.jr): Figure out if we need --no-fetch; remove if not.
  parser.add_argument(
      '--no-fetch', action='store_true',
      help='Disable automatic fetching')
  parser.add_argument(
      '--bootstrap-script',
      help='Path to the script used to bootstrap this tool (internal use only)')
  parser.add_argument('-O', '--project-override', metavar='ID=PATH',
      action=ProjectOverrideAction,
      help='Override a project repository path with a local one.')
  parser.add_argument(
      '--use-bootstrap', action='store_true',
      help='Use bootstrap/bootstrap.py to create a isolated python virtualenv'
           ' with required python dependencies.')
  parser.add_argument(
      '--operational-args-path', action='store',
      help='The path to an operational Arguments file. If provided, this file '
           'must contain a JSONPB-encoded Arguments protobuf message, and will '
           'be integrated into the runtime parameters.')

  subp = parser.add_subparsers()

  fetch_p = subp.add_parser(
      'fetch',
      description='Fetch and update dependencies.')
  fetch_p.set_defaults(command='fetch')

  simulation_test_p = subp.add_parser(
    'simulation_test',
    description='Generate or check expectations by simulation')
  simulation_test_p.set_defaults(command='simulation_test')
  simulation_test_p.add_argument('args')

  lint_p = subp.add_parser(
      'lint',
      description='Check recipes for stylistic and hygenic issues')
  lint_p.set_defaults(command='lint')

  lint_p.add_argument(
      '--whitelist', '-w', action='append',
      help='A regexp matching module names to add to the default whitelist. '
           'Use multiple times to add multiple patterns,')

  run_p = subp.add_parser(
      'run',
      description='Run a recipe locally')
  run_p.set_defaults(command='run')
  run_p.add_argument(
      '--properties-file',
      help='A file containing a json blob of properties')
  run_p.add_argument(
      '--properties',
      help='A json string containing the properties')
  run_p.add_argument(
      '--workdir',
      help='The working directory of recipe execution')
  run_p.add_argument(
      '--output-result-json',
      help='The file to write the JSON serialized returned value \
            of the recipe to')
  run_p.add_argument(
      'recipe',
      help='The recipe to execute')
  run_p.add_argument(
      'props',
      nargs=argparse.REMAINDER,
      help='A list of property pairs; e.g. mastername=chromium.linux '
           'issue=12345')
  run_p.add_argument(
      '--timestamps',
      action='store_true',
      help='If true, emit CURRENT_TIMESTAMP annotations. '
           'Default: false. '
           'CURRENT_TIMESTAMP annotation has one parameter, current time in '
           'Unix timestamp format. '
           'CURRENT_TIMESTAMP annotation will be printed at the beginning and '
           'end of the annotation stream and also immediately before each '
           'STEP_STARTED and STEP_CLOSED annotations.',
  )

  remote_p = subp.add_parser(
      'remote',
      description='Invoke a recipe command from specified repo and revision')
  remote_p.set_defaults(command='remote')
  remote_p.add_argument(
      '--repository', required=True,
      help='URL of a git repository to fetch')
  remote_p.add_argument(
      '--revision',
      help='Git commit hash to check out; defaults to latest revision (HEAD)')
  remote_p.add_argument(
      '--workdir',
      help='The working directory of repo checkout')
  remote_p.add_argument(
      '--use-gitiles', action='store_true',
      help='Use Gitiles-specific way to fetch repo (potentially cheaper for '
           'large repos)')
  remote_p.add_argument(
      'remote_args', nargs='*',
      help='Arguments to pass to fetched repo\'s recipes.py')

  autoroll_p = subp.add_parser(
      'autoroll',
      help='Roll dependencies of a recipe package forward (implies fetch)')
  autoroll_p.set_defaults(command='autoroll')
  autoroll_p.add_argument(
      '--output-json',
      help='A json file to output information about the roll to.')
  autoroll_p.add_argument(
      '--projects', action='append', default=None,
      help='Projects we care about rolling. Any project which has a rejected'
        'roll which isn\'t part of this set will be ignored, when computing'
        'rejected candidates.')


  depgraph_p = subp.add_parser(
      'depgraph',
      description=(
          'Produce graph of recipe and recipe module dependencies. Example: '
          './recipes.py --package infra/config/recipes.cfg depgraph | tred | '
          'dot -Tpdf > graph.pdf'))
  depgraph_p.set_defaults(command='depgraph')
  depgraph_p.add_argument(
      '--output', type=argparse.FileType('w'), default=sys.stdout,
      help='The file to write output to')
  depgraph_p.add_argument(
      '--ignore-package', action='append', default=[],
      help='Ignore a recipe package (e.g. recipe_engine). Can be passed '
           'multiple times')
  depgraph_p.add_argument(
      '--recipe-filter', default='',
      help='A recipe substring to examine. If present, the depgraph will '
           'include a recipe section containing recipes whose names contain '
           'this substring. It will also filter all nodes of the graph to only '
           'include modules touched by the filtered recipes.')

  refs_p = subp.add_parser(
      'refs',
      description='List places referencing given recipe module(s).')
  refs_p.set_defaults(command='refs')
  refs_p.add_argument('modules', nargs='+', help='Module(s) to query for')
  refs_p.add_argument('--transitive', action='store_true',
                      help='Compute transitive closure of the references')

  doc_p = subp.add_parser(
      'doc',
      description='List all known modules reachable from the current package, '
          'with their documentation')
  doc_p.set_defaults(command='doc')

  info_p = subp.add_parser(
      'info',
      description='Query information about the current recipe package')
  info_p.set_defaults(command='info')
  info_p.add_argument(
      '--recipes-dir', action='store_true',
      help='Get the subpath where the recipes live relative to repository root')

  args = parser.parse_args()

  # Load/parse operational arguments.
  op_args = arguments_pb2.Arguments()
  if args.operational_args_path is not None:
    with open(args.operational_args_path) as fd:
      data = fd.read()
    jsonpb.Parse(data, op_args)

  if args.use_bootstrap and not os.environ.pop('RECIPES_RUN_BOOTSTRAP', None):
    subprocess.check_call(
        [sys.executable, 'bootstrap/bootstrap.py', '--deps-file',
         'bootstrap/deps.pyl', 'ENV'],
        cwd=os.path.dirname(os.path.realpath(__file__)))

    os.environ['RECIPES_RUN_BOOTSTRAP'] = '1'
    args = sys.argv
    return subprocess.call(
        [os.path.join(
            os.path.dirname(os.path.realpath(__file__)), 'ENV/bin/python'),
         os.path.join(ROOT_DIR, 'recipes.py')] + original_sys_argv[1:])

  if args.verbose > 1:
    logging.getLogger().setLevel(logging.DEBUG)
  elif args.verbose > 0:
    logging.getLogger().setLevel(logging.INFO)

  # Commands which do not require config_file, package_deps, and other objects
  # initialized later.
  if args.command == 'remote':
    return remote(args)

  repo_root, config_file = get_package_config(args)

  try:
    # TODO(phajdan.jr): gracefully handle inconsistent deps when rolling.
    # This fails if the starting point does not have consistent dependency
    # graph. When performing an automated roll, it'd make sense to attempt
    # to automatically find a consistent state, rather than bailing out.
    # Especially that only some subcommands refer to package_deps.
    package_deps = package.PackageDeps.create(
        repo_root, config_file, allow_fetch=not args.no_fetch,
        deps_path=args.deps_path, overrides=args.project_override)
  except subprocess.CalledProcessError:
    # A git checkout failed somewhere. Return 2, which is the sign that this is
    # an infra failure, rather than a test failure.
    return 2

  if args.command == 'fetch':
    # We already did everything in the create() call above.
    assert not args.no_fetch, 'Fetch? No-fetch? Make up your mind!'
    return 0
  if args.command == 'simulation_test':
    return simulation_test(package_deps, args)
  elif args.command == 'lint':
    return lint(package_deps, args)
  elif args.command == 'run':
    return run(package_deps, args, op_args)
  elif args.command == 'autoroll':
    return autoroll(args)
  elif args.command == 'depgraph':
    return depgraph(package_deps, args)
  elif args.command == 'refs':
    return refs(package_deps, args)
  elif args.command == 'doc':
    return doc(package_deps, args)
  elif args.command == 'info':
    return info(args)
  else:
    print """Dear sir or madam,
        It has come to my attention that a quite impossible condition has come
    to pass in the specification you have issued a request for us to fulfill.
    It is with a heavy heart that I inform you that, at the present juncture,
    there is no conceivable next action to be taken upon your request, and as
    such, we have decided to abort the request with a nonzero status code.  We
    hope that your larger goals have not been put at risk due to this
    unfortunate circumstance, and wish you the best in deciding the next action
    in your venture and larger life.

    Warmly,
    recipes.py
    """
    return 1

  return 0

if __name__ == '__main__':
  # Use os._exit instead of sys.exit to prevent the python interpreter from
  # hanging on threads/processes which may have been spawned and not reaped
  # (e.g. by a leaky test harness).
  try:
    ret = main()
  except Exception as e:
    import traceback
    traceback.print_exc(file=sys.stderr)
    print >> sys.stderr, 'Uncaught exception (%s): %s' % (type(e).__name__, e)
    sys.exit(1)

  if not isinstance(ret, int):
    if ret is None:
      ret = 0
    else:
      print >> sys.stderr, ret
      ret = 1
  sys.stdout.flush()
  sys.stderr.flush()
  os._exit(ret)
