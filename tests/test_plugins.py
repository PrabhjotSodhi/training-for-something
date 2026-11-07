import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class PluginsTest(unittest.TestCase):
    def test_marketplace_points_to_the_named_plugin(self) -> None:
        marketplace = json.loads(
            (ROOT / ".claude-plugin/marketplace.json").read_text(encoding="utf-8")
        )
        self.assertEqual(marketplace["name"], "training-for-something")
        self.assertEqual(marketplace["owner"], {"name": "Training For Something"})
        self.assertEqual(
            marketplace["plugins"],
            [{"name": "tfs", "source": "./plugins/claude-code"}],
        )
        plugin_root = ROOT / marketplace["plugins"][0]["source"]
        manifest = json.loads(
            (plugin_root / ".claude-plugin/plugin.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["name"], "tfs")
        self.assertTrue((plugin_root / "skills/tfs/SKILL.md").is_file())

    def test_mcp_starts_the_local_tfs_command(self) -> None:
        config = json.loads((ROOT / "plugins/claude-code/.mcp.json").read_text(encoding="utf-8"))
        self.assertEqual(config, {"mcpServers": {"tfs": {"command": "tfs", "args": ["mcp"]}}})

    def test_both_clients_get_the_same_skill(self) -> None:
        claude = (ROOT / "plugins/claude-code/skills/tfs/SKILL.md").read_bytes()
        codex = (ROOT / "plugins/codex/tfs/SKILL.md").read_bytes()
        self.assertEqual(claude, codex)
        _, frontmatter, body = claude.decode("utf-8").split("---", 2)
        fields = dict(line.split(": ", 1) for line in frontmatter.strip().splitlines())
        self.assertEqual(fields["name"], "tfs")
        self.assertTrue(fields["description"].strip())
        self.assertTrue(body.strip())
