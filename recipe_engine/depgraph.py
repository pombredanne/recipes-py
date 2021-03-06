# Copyright 2016 The LUCI Authors. All rights reserved.
# Use of this source code is governed under the Apache License, Version 2.0
# that can be found in the LICENSE file.

from __future__ import print_function

import sys
import os
import collections

from . import loader


_GRAPH_HEADER = """strict digraph {
  concentrate = true;
  ranksep = 2;
  nodesep = 0.25;
"""

_GRAPH_FOOTER = """}
"""


def main(universe, own_package, ignore_packages, stdout, recipe_filter):
  module_to_package = {}

  # All deps maps a tuple of (is_recipe, id) to deps (list of ids). is_recipe is
  # a boolean, all ids are strings.
  all_deps = {}
  for package, module_name in universe.loop_over_recipe_modules():
    if package in ignore_packages:
      continue
    mod = universe.load(package, module_name)

    all_deps[(False, mod.NAME)] = mod.LOADED_DEPS
    module_to_package[mod.NAME] = package.name

  if recipe_filter:
    recipe_to_package = {}
    universe_view = loader.UniverseView(universe, own_package)
    for _, recipe_name in universe_view.loop_over_recipes():
      if recipe_filter not in recipe_name:
        continue

      recipe = universe_view.load_recipe(recipe_name)

      all_deps[(True, recipe_name)] = recipe.LOADED_DEPS
      recipe_to_package[recipe_name] = own_package

    # If we actually found any recipes
    if recipe_to_package:
      # Prune anything our recipe doesn't see via BFS.
      queue = [
          (True, name) for (is_recipe, name), _ in all_deps.items()
            if is_recipe]

      to_keep = set()
      while queue:
        itm = queue.pop()
        to_keep.add(itm)
        for dep in all_deps[itm]:
          queue.append((False, dep))

      all_deps = {key: deps for key, deps in all_deps.items() if key in to_keep}

      mod_names = [
          name for (is_recipe, name), _ in all_deps.items() if not is_recipe]
      module_to_package = {
          m_name: p_name for m_name, p_name in module_to_package.items()
          if m_name in mod_names}

      recipe_names = [
          name for (is_recipe, name), _ in all_deps.items() if is_recipe]
      recipe_to_package = {
          r_name: p_name for r_name, p_name in recipe_to_package.items()
          if r_name in recipe_names}


  print(_GRAPH_HEADER, file=stdout)
  edges = []
  for (is_recipe, name), deps in all_deps.items():
    for dep in deps:
      edges.append(((is_recipe, name), dep))

  for edge in edges:
    (is_recipe, first_name), second_name = edge

    if not is_recipe:
      if module_to_package[first_name] in ignore_packages:
        continue
    else:
      if recipe_to_package[first_name] in ignore_packages:
        continue
      first_name = 'recipe ' + first_name

    if module_to_package[second_name] in ignore_packages:
      continue

    print('  "%s" -> "%s"' % (first_name, second_name), file=stdout)

  packages = {}
  for module, package in module_to_package.iteritems():
    packages.setdefault(package, []).append(module)
  for package, modules in packages.iteritems():
    if package in ignore_packages:
      continue
    # The "cluster_" prefix has magic meaning for graphviz and makes it
    # draw a box around the subgraph.
    print('  subgraph "cluster_%s" { label="%s"; %s; }' % (
        package, package, '; '.join(modules)), file=stdout)

  if recipe_filter and recipe_to_package:
    recipe_names = [
        '"recipe %s"' % name for name in recipe_to_package.keys()]
    print('  subgraph "cluster_recipes" { label="recipes"; %s; }' % (
        '; '.join(recipe_names)), file=stdout)

  print(_GRAPH_FOOTER, file=stdout)
