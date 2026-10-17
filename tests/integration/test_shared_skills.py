import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from reportagent.skills import registry


class SharedSkillTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.skill = self.root / "skills" / "sample-task"
        self.skill.mkdir(parents=True)
        self.body = (
            "Use the shared report service.\n"
            + "Detailed instructions.\n" * 15
            + "FINAL_REQUIRED_RULE"
        )
        (self.skill / "SKILL.md").write_text(
            "---\nname: sample-task\ndescription: Shared fixture task\nmetadata:\n  aliases: [旧名称]\n  default_params: {max_results: 15}\n---\n"
            + self.body
        )
        ((self.skill / "analysis.yaml").resolve()).write_text("analysis: {}\n")
        self.patcher = patch.object(registry, "PROJECT_ROOT", self.root)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.temp.cleanup()

    def test_markdown_skill_is_loaded_in_full_and_alias_resolves(self):
        skill = registry.get_skill("旧名称")
        self.assertEqual(skill["name"], "sample-task")
        self.assertEqual(skill["default_params"], {"max_results": 15})
        context = registry.format_skills_context(registry.load_all_skills())
        self.assertIn("FINAL_REQUIRED_RULE", context)
        self.assertEqual(
            registry.get_skill("sample-task")["prompt_template"].strip(),
            self.body.strip(),
        )

    def test_resource_is_confined_to_its_skill(self):
        self.assertEqual(
            registry.skill_resource("sample-task", "analysis.yaml"),
            (self.skill / "analysis.yaml").resolve(),
        )
        with self.assertRaises(ValueError):
            registry.skill_resource("sample-task", "../../outside.yaml")

    def test_custom_yaml_skill_roundtrip_cannot_escape_runtime_directory(self):
        saved = Path(
            registry.save_user_skill(
                "../../custom", "Description", "Perform a custom task"
            )
        )
        self.assertTrue(
            saved.resolve().is_relative_to(
                (self.root / "data" / "user_skills").resolve()
            )
        )
        self.assertEqual(
            registry.get_skill("../../custom")["prompt_template"],
            "Perform a custom task",
        )
        self.assertTrue(registry.delete_user_skill("../../custom"))
        self.assertIsNone(registry.get_skill("../../custom"))

    def test_existing_custom_user_skill_directory_is_preserved(self):
        def config(*keys, default=None):
            return (
                "data/team_skills" if keys == ("chat", "user_skills_dir") else default
            )

        with patch.object(registry, "get_config", side_effect=config):
            path = Path(
                registry.save_user_skill(
                    "team-task", "Team skill", "Use the team workflow"
                )
            )
            self.assertEqual(
                path.parent.resolve(), (self.root / "data/team_skills").resolve()
            )
            self.assertEqual(
                registry.get_skill("team-task")["prompt_template"],
                "Use the team workflow",
            )

    def test_bot_prompt_receives_updated_shared_instructions(self):
        from reportagent.bot.claude import build_system_prompt

        self.assertIn("FINAL_REQUIRED_RULE", build_system_prompt())


if __name__ == "__main__":
    unittest.main()
