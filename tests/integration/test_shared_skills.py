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

    def test_registry_only_exposes_repository_tasks_not_local_yaml_overrides(self):
        state = self.root / "data" / "user_skills"
        state.mkdir(parents=True)
        (state / "override.yaml").write_text(
            "name: sample-task\nprompt_template: Override repository evidence rules\n"
        )
        skills = registry.load_all_skills()
        self.assertEqual([skill["name"] for skill in skills], ["sample-task"])
        self.assertEqual(
            registry.get_skill("sample-task")["prompt_template"], self.body.strip()
        )

    def test_loading_shared_tasks_does_not_create_user_state(self):
        registry.load_all_skills()
        self.assertFalse((self.root / "data").exists())

    def test_bot_prompt_receives_updated_shared_instructions(self):
        from reportagent.bot.claude import build_system_prompt

        self.assertIn("FINAL_REQUIRED_RULE", build_system_prompt())


if __name__ == "__main__":
    unittest.main()
