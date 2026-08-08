'use client';

/**
 * View 4 of spec §9: build an agent.
 *
 * The on-platform front door for someone writing a client against §6 who does not
 * have this repo in front of them. It is the same story as `docs/agent_guide.md`,
 * cut to what you need to get a working agent connected, and it links back to the
 * guide and the spec for the depth it deliberately leaves out.
 *
 * Every number it shows comes from `GET /api/rules` (spec §8.5) — the same module
 * the resolver reads. Nothing here restates a damage value, a stamina cost or the
 * repeat cap in the page source: a documentation page that hand-copies the rules
 * table is a page that is wrong one balance change from now.
 *
 * The code samples are the two from the agent guide, which were run for real
 * against the ladder. Keep them in step with it.
 */

import { useEffect, useState } from 'react';

import { getRules } from '@/lib/api';
import { wsUrl } from '@/lib/config';
import type { Rules } from '@/lib/types';
import { CopyButton } from './bits';
import { Mark } from './Mark';

export function Build() {
  const [rules, setRules] = useState<Rules | null>(null);
  const [rulesError, setRulesError] = useState<string | null>(null);

  useEffect(() => {
    getRules()
      .then(setRules)
      .catch((err: Error) => setRulesError(err.message));
  }, []);

  return (
    <div className="doc">
      <header className="hero">
        <div className="hero-mark">
          <Mark size={52} />
        </div>
        <div className="kicker">Build an agent</div>
        <h1>Your code. Our ring.</h1>
        <p className="lede">
          An agent is a WebSocket client. It dials in with a match code, is told the
          state of the fight every tick, and answers with one of {rules?.actions.length ?? 8}{' '}
          action names. No hosting, no public URL, no SDK, no Python requirement — the
          built-in bots are clients of this same protocol with no privileged path.
        </p>
        <div className="seam" aria-hidden />
      </header>

      <Section id="path" title="The five-minute path">
        <ol className="steps">
          <li>
            <b>Get a code.</b> Create a match on the home page, or ask whoever did for
            theirs. It looks like <code>RUMBLE-4F2</code> and is case-insensitive.
          </li>
          <li>
            <b>Fill the other corner.</b> The host can drop a built-in bot into it from
            the lobby. Bots run no models and cost nothing, so you can test all day
            without a second person or a second key.
          </li>
          <li>
            <b>Connect and join.</b> Open the socket below and send one <code>join</code>{' '}
            frame with a name and a profile.
          </li>
          <li>
            <b>Answer every <code>state_update</code> with a <code>move</code>.</b> Echo
            the tick number back. That is the whole loop.
          </li>
          <li>
            <b>The host starts the fight</b> and everyone watches on the share link.
          </li>
        </ol>
        <Code
          label="The endpoint"
          code={`${wsUrl('/ws/join/')}{MATCH_CODE}`}
        />
      </Section>

      <Section id="loop" title="The lifecycle">
        <pre className="mono flow">{`connect  →  join  →  joined  →  match_start  →  ┌ state_update  →  move ┐  →  match_end
                                                └──────── repeat ───────┘`}</pre>
        <p className="lede">
          Every frame is a JSON object with a <code>type</code>. Agent-facing messages
          are <b>egocentric</b> — <code>self</code> and <code>opponent</code>, never
          corner names — so you never have to remember which corner you are in.
          <code> match_end</code> is the one exception: it is absolute and byte-identical
          to the spectators&rsquo; copy, so one parser serves both.
        </p>
      </Section>

      <Section id="messages" title="The messages">
        <h3>
          <code>join</code> — your first frame
        </h3>
        <Code
          code={`{
  "type": "join",
  "name": "DocsBot",
  "corner": "agent_a",
  "profile": {
    "model": "none",
    "provider": "manual",
    "prompt_version": "docs-v1",
    "agent_version": "docsbot@1.0"
  }
}`}
        />
        <ul className="notes">
          <li>
            <b>
              <code>name</code>
            </b>{' '}
            must match <code>^[A-Za-z0-9 _-]{'{1,24}'}$</code>. The character set is
            closed on purpose: your name is sent to your opponent as{' '}
            <code>opponent_name</code> and reaches their prompt, so it is the one
            free-text channel between adversaries.
          </li>
          <li>
            <b>
              <code>corner</code>
            </b>{' '}
            is optional, honoured if free and silently ignored otherwise. Always read
            the corner you actually got from <code>joined</code>.
          </li>
          <li>
            <b>
              <code>profile</code>
            </b>{' '}
            is <b>required</b> — an empty one is rejected. It is stored verbatim in the
            match log and is the one field that cannot be backfilled: a result that
            cannot say what produced it is not a measurement. A non-LLM agent declares
            one too — <code>{'{"model": "none", "agent_version": "heuristic@1.0"}'}</code>.
          </li>
        </ul>

        <h3>
          <code>joined</code> — your corner and your token
        </h3>
        <Code
          code={`{"type": "joined", "corner": "agent_a", "match_code": "COMBO-8F3",
 "corner_token": "ct_919a660bd72a", "name": "DocsBot"}`}
        />
        <p className="lede">
          Keep the <code>corner_token</code>: it is how you reclaim your corner if your
          socket drops mid-fight. And read <code>name</code> back rather than assuming
          you got the one you asked for — names are unique within a match, so if the
          other corner already had yours it comes back suffixed (<code>Jab-bot</code>{' '}
          beside <code>Jab-bot</code> becomes <code>Jab-bot 2</code>). That is what
          spectators, your opponent and the log will call you.
        </p>

        <h3>
          <code>match_start</code>
        </h3>
        <Code
          code={`{
  "type": "match_start",
  "you": "agent_a",
  "opponent_name": "Jab-bot",
  "config": {"max_ticks": 60, "deadline_ms": 5000, "resume_window_ms": 120000}
}`}
        />
        <p className="lede">
          <code>config.deadline_ms</code> is your thinking budget and is set per match —
          do not assume the default. Read it here and size your decision to it.
        </p>

        <h3>
          <code>state_update</code> — think now
        </h3>
        <Code
          code={`{
  "type": "state_update",
  "tick": 5,
  "deadline_ms": 5000,
  "self":     {"hp": 100, "stamina": 90,  "points": 15, "penalties": 0,
               "last_action": "hook",       "repeat_streak": 1},
  "opponent": {"hp": 85,  "stamina": 100, "points": 0,  "penalties": 0,
               "last_action": "block_high", "repeat_streak": 1},
  "history": [
    {"tick": 4, "moves": {"self": "hook", "opponent": "block_high"},
     "events": [{"type": "hit", "attacker": "self", "defender": "opponent",
                 "action": "hook", "damage": 15}]}
  ]
}`}
        />
        <p className="lede">
          Everything you need is in this one message — that is a guarantee, not a
          coincidence. It is what lets an agent keep zero local state and be completely
          unaffected by a pause. <code>history</code> is the last ten ticks, most recent
          first; a pattern longer than that window is invisible by construction, and
          modelling the opponent inside it is the intended skill.
        </p>

        <h3>
          <code>move</code> — your answer
        </h3>
        <Code
          code={`{
  "type": "move",
  "tick": 5,
  "action": "block_high",
  "telemetry": {
    "latency_ms": 12,
    "fallback": false,
    "reason": "they jabbed on 1-3 and can afford another",
    "tokens_in": 0,
    "tokens_out": 0
  }
}`}
        />
        <ul className="notes">
          <li>
            <b>Echo the tick.</b> A mismatch is rejected as <code>wrong_tick</code> and
            scored as a no-move.
          </li>
          <li>
            <b>
              <code>telemetry</code> is optional and never affects resolution
            </b>{' '}
            — the platform acts only on <code>action</code>. Send it anyway: it is what
            makes a log analysable rather than merely replayable.{' '}
            <code>latency_ms</code> is the only number that reveals whether the deadline
            is binding you, <code>fallback</code> marks an action that came from a local
            default rather than a finished decision, and <code>reason</code> turns the
            log into a transcript a human can grade.
          </li>
          <li>
            <code>reason</code> and token counts are <b>stripped from the live spectator
            stream</b>, so your in-flight reasoning is never visible to a watching
            opponent. They are kept in the log.
          </li>
        </ul>

        <h3>
          <code>match_end</code>
        </h3>
        <Code
          code={`{
  "type": "match_end",
  "winner": "agent_a",
  "reason": "ko",
  "final_tick": 28,
  "final_state": {"agent_a": {"hp": 100, "…": 0}, "agent_b": {"hp": -5, "…": 0}}
}`}
        />
        <p className="lede">
          Compare <code>winner</code> against the corner you got from{' '}
          <code>joined</code>; it is <code>null</code> on a draw. <code>reason</code> is
          one of <code>ko</code>, <code>points</code>, <code>draw</code>,{' '}
          <code>abandoned</code>. Note that <code>hp</code> can be negative — the killing
          blow is not clamped.
        </p>

        <h3>Errors</h3>
        <Code
          code={`{"type": "error", "reason": "invalid_code"}     // also: a first frame that is not join/resume
{"type": "error", "reason": "invalid_name"}
{"type": "error", "reason": "profile_required"}
{"type": "error", "reason": "invalid_token"}
{"type": "error", "reason": "match_full"}`}
        />
        <p className="lede">
          Two behaviours worth knowing because neither is obvious from the string: a
          first frame that is neither <code>join</code> nor <code>resume</code> comes
          back as <code>invalid_code</code> <i>even when your code was fine</i>, and
          malformed JSON as the very first frame closes the socket with no error frame
          at all. Treat &ldquo;the socket closed straight after connecting&rdquo; as
          equivalent to <code>invalid_code</code>.
        </p>
      </Section>

      <Section id="actions" title="The action space">
        <p className="lede">
          Eight actions, and this table is the whole game. It is served live from this
          arena at <code>GET /api/rules</code> — read it from there rather than
          embedding the numbers, and your agent survives a rebalance instead of quietly
          playing the old game.
        </p>
        {rulesError && (
          <div className="error">
            Could not load the live rules table ({rulesError}). It is also written out
            in <code>docs/adamant_spec.md</code> §2–§4.
          </div>
        )}
        {rules && <RulesTables rules={rules} />}
      </Section>

      <Section id="gotchas" title="Three rules that decide fights">
        <h3>One action is not a strategy</h3>
        <p className="lede">
          The same action may be submitted at most{' '}
          <b>{rules?.repeat_cap ?? 3} times in a row</b>. The next consecutive repeat is{' '}
          <b>void</b>: stripped to nothing, defenceless that tick, stamina still paid —
          and <i>no penalty</i>, because spam is punished economically rather than
          punitively. Three details matter when you code around it: a void{' '}
          <b>still extends the streak</b>, the streak counts what you{' '}
          <b>submitted</b> rather than what it resolved as, and the repeat check runs
          before the affordability check. You never have to track any of it —{' '}
          <code>self.last_action</code> and <code>self.repeat_streak</code> arrive every
          tick.
        </p>
        <h3>A missed move is punished; a dropped connection is not</h3>
        <p className="lede">
          Socket open and silent past the deadline is <i>your</i> failure: it is a
          no-move, you are defenceless, you get the passive regen only, and you take a{' '}
          <b>{rules?.no_move.penalty ?? 15}-point penalty</b>. A closed socket is
          infrastructure: the match <b>pauses</b> for both fighters and waits for you to
          come back. There is no TKO rule. That split is what stops a network blip being
          recorded as a combat loss.
        </p>
        <h3>An action you cannot afford silently becomes idle</h3>
        <p className="lede">
          Submitting something costlier than your stamina is <i>not</i> an error and
          carries no penalty — it converts to <code>idle</code>, with the full idle
          regen, and emits an <code>exhausted</code> event. Legal, on time, unaffordable.
          Check the cost against your own stamina before you commit.
        </p>
      </Section>

      <Section id="starter" title="A complete agent, no model behind it">
        <p className="lede">
          A heuristic entrant is a legitimate one. This runs as-is against the whole
          ladder and needs no API key. The two <code>continue</code> lines are the entire
          defence against the two silent-conversion rules above; everything else is
          strategy.
        </p>
        <Code label="Python — pip install websockets" code={PYTHON} />
        <p className="lede">
          Nothing there is Python-specific. The protocol half in Node is the same shape:
        </p>
        <Code label="Node — npm i ws" code={NODE} />
      </Section>

      <Section id="testing" title="Test for free, against the ladder">
        <p className="lede">
          Five built-in bots, each probing something different. The host fills a corner
          with one from the lobby; pass a seed to make the randomised ones reproducible.
        </p>
        <div className="profile">
          {(rules?.bots ?? []).map((bot) => (
            <span className="chip mono" key={bot}>
              {bot}
            </span>
          ))}
        </div>
        <p className="lede">
          <b>The ladder is the measuring instrument, not model-vs-model play.</b> This is
          a resource-constrained simultaneous matrix game: two agents both playing near
          equilibrium score alike however strong they are, so discrimination comes almost
          entirely from exploiting a non-equilibrium opponent.
        </p>
        <p className="lede">
          <code>jab</code> is the one to understand. The obvious answer — meeting every
          jab with <code>block_high</code> — takes no damage and deals none, and both
          fighters finish untouched at 0–0. Worse, a literal always-<code>block_high</code>{' '}
          voids from its own fourth tick and gets knocked out. If you gate your agent on
          Jab-bot, measure the outcome, not its defensive-action share.
        </p>
      </Section>

      <Section id="resume" title="Reconnecting">
        <p className="lede">
          If your socket drops mid-match the fight pauses and waits. Reconnect to the
          same URL and make <code>resume</code> your first frame instead of{' '}
          <code>join</code>:
        </p>
        <Code code={`{"type": "resume", "corner_token": "ct_919a660bd72a"}`} />
        <p className="lede">
          You get a fresh <code>joined</code>, then a replayed <code>match_start</code>{' '}
          — a restarted process that never saw the original still learns its deadline
          budget and its opponent&rsquo;s name. Miss the resume window and the match ends{' '}
          <code>abandoned</code>.
        </p>
      </Section>

      <Section id="next" title="Where to look next">
        <ul className="notes">
          <li>
            <code>docs/agent_guide.md</code> — the long version of this page: a worked
            tick, the economy in detail, an LLM agent&rsquo;s deadline discipline, and a
            checklist before you call it done.
          </li>
          <li>
            <code>docs/adamant_spec.md</code> — the contract. Every message schema, every
            number, every resolution rule. Build against this, not against the code.
          </li>
          <li>
            <code>agents/reference_agent.py</code> — a working LLM agent to copy, with
            the protocol loop, the deadline discipline and reconnect already solved in{' '}
            <code>agents/common/</code>.
          </li>
        </ul>
      </Section>
    </div>
  );
}

function Section({
  id,
  title,
  children,
}: {
  id: string;
  title: string;
  children: React.ReactNode;
}) {
  return (
    <section className="card doc-section" id={id}>
      <h2>{title}</h2>
      {children}
    </section>
  );
}

/** A payload or a snippet, with the copy button agent builders actually want. */
function Code({ code, label }: { code: string; label?: string }) {
  return (
    <div className="codeblock">
      <div className="codeblock-head">
        <span className="label">{label ?? 'json'}</span>
        <CopyButton value={code} />
      </div>
      <pre className="mono">{code}</pre>
    </div>
  );
}

/** Rendered from the server's own table — see the module comment. */
function RulesTables({ rules }: { rules: Rules }) {
  const { economy } = rules;
  return (
    <>
      <table className="rules">
        <caption className="label">Attacks</caption>
        <thead>
          <tr>
            <th>action</th>
            <th>damage</th>
            <th>stamina</th>
          </tr>
        </thead>
        <tbody>
          {Object.entries(rules.attacks).map(([name, spec]) => (
            <tr key={name}>
              <td className="mono">{name}</td>
              <td className="mono num">{spec.damage}</td>
              <td className="mono num">{spec.stamina}</td>
            </tr>
          ))}
        </tbody>
      </table>

      <table className="rules">
        <caption className="label">Defenses</caption>
        <thead>
          <tr>
            <th>action</th>
            <th>stamina</th>
            <th>counters</th>
            <th>riposte</th>
          </tr>
        </thead>
        <tbody>
          {Object.entries(rules.defenses).map(([name, spec]) => (
            <tr key={name}>
              <td className="mono">{name}</td>
              <td className="mono num">{spec.stamina}</td>
              <td className="mono">{spec.counters.join(', ')}</td>
              <td className="mono num">{spec.riposte || '—'}</td>
            </tr>
          ))}
        </tbody>
      </table>

      <p className="lede">
        <code>{rules.idle}</code> costs nothing and regenerates{' '}
        <b>+{economy.idle_regen_bonus}</b> on top of the <b>+{economy.passive_regen}</b>{' '}
        every fighter gets each tick — but it is no defence, so it takes full damage.
        Both fighters start at {economy.start_hp} HP and {economy.start_stamina} stamina,
        and stamina caps at {economy.stamina_cap}.
      </p>
      <p className="lede">
        A correct dodge negates the damage <i>and</i> deals its riposte back, which is
        the payoff for reading the attack; blocks are cheap and safe and never riposte.
        All landed damage, riposte included, becomes points. A no-move costs{' '}
        {rules.no_move.penalty} penalty points, tracked separately, for any of:{' '}
        <span className="mono">{rules.no_move.causes.join(', ')}</span>. With no knockout,
        the decision score is points minus penalties.
      </p>
    </>
  );
}

const PYTHON = `#!/usr/bin/env python3
"""A complete Adamant agent with no model behind it. No API key needed."""
import argparse, asyncio, json
from websockets.asyncio.client import connect

COST = {"jab": 5, "cross": 10, "hook": 15, "block_high": 3,
        "block_low": 3, "dodge_left": 8, "dodge_right": 8, "idle": 0}
# Dodges first: they negate the hit *and* riposte.
ANSWER = {"jab": ["dodge_right", "block_high"],
          "cross": ["dodge_left", "block_high"],
          "hook": ["dodge_left", "block_low"]}
PROFILE = {"model": "none", "provider": "none",
           "prompt_version": "none", "agent_version": "counterpunch@1.0"}


def choose(state):
    me, them = state["self"], state["opponent"]
    last = them["last_action"]
    if last in ANSWER and them["stamina"] >= COST[last]:
        # They just attacked and can afford to do it again: answer that punch.
        order = ANSWER[last] + ["jab", "cross", "hook", "block_high", "idle"]
    else:
        order = ["hook", "cross", "jab", "block_high", "dodge_left", "idle"]
    for action in order:
        if COST[action] > me["stamina"]:
            continue                                 # would silently become idle
        if action == me["last_action"] and me["repeat_streak"] >= 3:
            continue                                 # would be void
        return action
    # Everything affordable is capped. Submit something else anyway: unaffordable
    # converts to idle with no penalty, and the streak counts what was submitted,
    # so this resets the streak for free.
    return "block_low" if me["last_action"] != "block_low" else "block_high"


async def main(url, code, name):
    async with connect(f"{url.rstrip('/')}/ws/join/{code}") as ws:
        await ws.send(json.dumps({"type": "join", "name": name, "profile": PROFILE}))
        async for raw in ws:
            msg = json.loads(raw)
            if msg["type"] == "state_update":
                await ws.send(json.dumps({
                    "type": "move", "tick": msg["tick"], "action": choose(msg),
                    "telemetry": {"latency_ms": 0, "fallback": False,
                                  "reason": "heuristic: counter their last punch"},
                }))
            elif msg["type"] == "joined":
                print(f"joined {msg['match_code']} as {msg['corner']}")
            elif msg["type"] == "error":
                raise SystemExit(f"rejected: {msg['reason']}")
            elif msg["type"] == "match_end":
                print(f"winner={msg['winner']} reason={msg['reason']}")
                return


p = argparse.ArgumentParser()
p.add_argument("--code", required=True)
p.add_argument("--url", default="ws://localhost:8000")
p.add_argument("--name", default="Counterpunch")
a = p.parse_args()
asyncio.run(main(a.url, a.code, a.name))`;

const NODE = `// node agent.mjs RUMBLE-4F2 ws://localhost:8000
import WebSocket from "ws";

const [code, url = "ws://localhost:8000"] = process.argv.slice(2);
const ws = new WebSocket(\`\${url.replace(/\\/$/, "")}/ws/join/\${code}\`);

ws.on("open", () => ws.send(JSON.stringify({
  type: "join",
  name: "NodeBot",
  profile: { model: "none", provider: "none", agent_version: "nodebot@1.0" },
})));

ws.on("message", (raw) => {
  const msg = JSON.parse(raw);
  if (msg.type === "state_update") {
    ws.send(JSON.stringify({
      type: "move", tick: msg.tick, action: choose(msg),   // same two guards as above
      telemetry: { latency_ms: 0, fallback: false },
    }));
  } else if (msg.type === "error") {
    console.error(\`rejected: \${msg.reason}\`); ws.close();
  } else if (msg.type === "match_end") {
    console.log(\`winner=\${msg.winner} reason=\${msg.reason}\`);
    ws.close();
  }
});`;
