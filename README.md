# Claude Research Skills

Skills for investment research with Claude Code.

## Install

Paste this sentence into Claude Code, either in the terminal or in the Code tab of the Claude desktop app:

> Install or update the skills from https://github.com/behavioralvalueinvestor/claude-research-skills. Download a fresh copy of the repo into a new temporary folder. For each folder inside its `skills/` folder, replace `~/.claude/skills/<same folder name>/` with an exact copy of it: delete that old folder if it exists, then copy the new one so its SKILL.md ends up at `~/.claude/skills/<same folder name>/SKILL.md`. Do not touch any other folder in `~/.claude/skills/`. Then list the full path of every file you installed.

Then quit and restart Claude Code or the desktop app, whichever you used. To update later, paste the same sentence again.

## Before your first run: put your email in

The SEC asks every program that downloads from EDGAR to identify itself with a contact email. Open `~/.claude/skills/10k-to-markdown/tenk_to_md.py`, find this line near the top:

    UA = "10k-to-markdown youremail@gmail.com"

and replace `youremail@gmail.com` with your own email address. Or ask Claude Code to do it for you. Reinstalling or updating the skill puts the placeholder back, so do this again after each update.

## What's inside

- **/10k-to-markdown**: downloads one 10-K from SEC EDGAR for a ticker and fiscal year, and saves it as a Markdown file on your computer, plus a small index of where each section starts and ends. Claude then reads only the sections it needs instead of the whole filing. Needs Python 3, with nothing else to install.

Example: `/10k-to-markdown MSFT 2025`

## License

Free to use under the MIT license. See [LICENSE](LICENSE).
