import ast
import json
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _moviepilot_v2_plugin_paths():
    versioned_plugins = json.loads(
        (REPOSITORY_ROOT / "package.v2.json").read_text(encoding="utf-8")
    )
    shared_plugins = json.loads(
        (REPOSITORY_ROOT / "package.json").read_text(encoding="utf-8")
    )

    plugin_ids = set(versioned_plugins)
    plugin_ids.update(
        plugin_id
        for plugin_id, plugin_info in shared_plugins.items()
        if plugin_info.get("v2") is True
    )

    for plugin_id in sorted(plugin_ids):
        plugin_directory = "plugins.v2" if plugin_id in versioned_plugins else "plugins"
        yield REPOSITORY_ROOT / plugin_directory / plugin_id.lower() / "__init__.py"


def _method_parameters(tree):
    parameters = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        parameters[node.name] = {
            argument.arg
            for argument in (*node.args.args, *node.args.kwonlyargs)
            if argument.arg not in {"self", "cls"}
        }
    return parameters


def test_scheduler_kwargs_do_not_contain_plugin_method_arguments():
    violations = []

    for plugin_path in _moviepilot_v2_plugin_paths():
        tree = ast.parse(plugin_path.read_text(encoding="utf-8"))
        method_parameters = _method_parameters(tree)

        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue

            fields = {
                key.value: value
                for key, value in zip(node.keys, node.values)
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            }
            function = fields.get("func")
            scheduler_kwargs = fields.get("kwargs")
            if not isinstance(function, ast.Attribute) or not isinstance(scheduler_kwargs, ast.Dict):
                continue

            keyword_names = {
                key.value
                for key in scheduler_kwargs.keys
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            }
            collisions = keyword_names & method_parameters.get(function.attr, set())
            if collisions:
                violations.append(
                    f"{plugin_path.relative_to(REPOSITORY_ROOT)}:{node.lineno} "
                    f"{', '.join(sorted(collisions))}"
                )

    assert not violations, (
        "MoviePilot 调度参数应放在 kwargs，插件方法参数应放在 func_kwargs：\n"
        + "\n".join(violations)
    )


def test_popular_subscribe_catalog_version_matches_source():
    catalog = json.loads(
        (REPOSITORY_ROOT / "package.json").read_text(encoding="utf-8")
    )
    plugin_path = REPOSITORY_ROOT / "plugins/popularsubscribe/__init__.py"
    tree = ast.parse(plugin_path.read_text(encoding="utf-8"))

    source_version = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "plugin_version"
            for target in node.targets
        ):
            continue
        if isinstance(node.value, ast.Constant):
            source_version = node.value.value
        break

    assert source_version == catalog["PopularSubscribe"]["version"]
