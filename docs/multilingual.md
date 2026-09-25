# Reaching a catalog in more languages than it was written in

An author writes the trigger phrases of a procedure once, in the language they
work in. The catalog is reached in more languages than that. Translating every
trigger by hand does not scale and rots the moment a trigger is edited, and
asking a model at turn time would spend a call on every turn.

So the paraphrases are produced once, outside a turn, and written to a file
beside the skill. The index reads that file alongside the declared triggers.
Nothing about this changes what one turn costs.

## The rule that governs it

No feature adds a model call to a turn.

Everything below follows from it. The derivation happens when a procedure is
published or when somebody runs the command; the reading happens from disk; the
file is stamped so that a stale one is ignored rather than trusted. If the only
way to reach a catalog in a further language were to ask a model during the
turn, the answer would be that the catalog is not reached in that language.

## The derived file

One file per skill version, at:

```
<catalog root>/<type>/<name>/v<version>/generated-examples.json
```

The name is `enrichment.filename` in `configs/runtime.json`. It sits inside the
version directory so that it travels with the version it describes, is copied
with it, and is removed with it.

```json
{
  "source_hash": "9c1f...",
  "languages": {
    "it": ["riavvia il servizio di ricerca", "puoi far ripartire payments"],
    "de": ["starte den checkout-dienst neu"]
  }
}
```

Nothing in it is edited by hand. The declared triggers stay the only authored
truth; these are derived from them and are what gives way when the two
disagree.

## The source hash

`source_hash` is a digest of the declared triggers the file was made from.

Every read compares it against the triggers as they are now. If it does not
match, the whole file is ignored — not partially trusted, not repaired. A file
written from triggers that have since been edited describes a procedure that no
longer exists, and a paraphrase of a phrase nobody declares any more is worse
than no paraphrase at all. The fix is always to regenerate.

That is also why the hash covers the triggers rather than the whole skill file:
editing the body of a runbook does not invalidate a paraphrase of how somebody
asks for it, and editing a trigger does.

## When it regenerates

Two occasions, and no others.

**At publication.** When a proposal is published and a provider is configured
and `enrichment.enabled` is on, the derived phrases of the new version are
written as part of the same operation — beside the package inside the change
request that publication composes, so they travel with it and land in the
catalog when a person applies it. Whatever happens is recorded in the
transition note and nowhere else: the composed package already stands, and
nothing about it is undone by a paraphrase that could not be produced.

**When somebody asks.** After a catalog is edited by hand, after a language is
added to the configuration, or on a schedule:

```sh
oska enrich run                              # every procedure missing a language
oska enrich run --language it --language de  # these languages, not the configured ones
oska enrich run --dry-run                    # what a pass would write, asking nothing
oska enrich run --json
oska enrich show runbook.service_restart     # declared first, derived after
```

Without `--language`, the languages come from `discovery.languages`. A pass
covers every published skill that declares triggers and is missing any of those
languages, one model call per skill, and reports one line per skill —
`written`, `skipped` or `failed` with the reason. One skill that fails does not
end the pass, because a catalog half enriched is still better routed than one
not enriched at all; the command exits non-zero if anything failed. `--dry-run` reports the same lists without asking a provider for
anything, which is how to find out what a pass would cost before paying for it.

`enrich run` needs a provider: `--provider openai_compatible`, or `--provider
scripted --script <file>` to exercise it offline, or the one the settings name.
There is nothing sensible to degrade to, so with the provider `none` the
command stops and says so. This is the one place that differs from a turn,
which degrades rather than stopping.

`enabled` is what the runtime consults, not what the command consults. A person
running `enrich run` is asking for the files whatever the flag says; with the
flag off they are written and ignored.

## What the model is asked for

The description of the procedure and the phrases its author wrote down, with
the instruction to write further phrases a person could use to ask for the same
thing, one set per language, keeping the meaning exactly and adding nothing the
declared phrases do not already say — the way a person asks for something, not
the way a document describes it.

What comes back is filtered before it is written. A phrase that repeats a
declared trigger is dropped, so is a repeat of another derived phrase, and each
language is cut to `enrichment.per_language`. A model that writes something
imaginative produces a phrase nobody matches, not a capability nobody declared:
these texts only ever raise the score of the procedure they belong to.

## What the index does with it

A skill is indexed under several texts — its description, each trigger, each
derived phrase, and each phrasing the improvement job has learned — and scores
the best of them rather than their average. More phrasings can therefore raise
a score and never dilute one. `matched_on` reports which text won, with its
language, which is how a routing surprise is explained: `oska enrich show`
prints the declared phrases and then the derived ones, and one of them is the
answer.

`enrichment.cap_per_route` bounds how many texts one skill contributes, and it
never cuts a declared one. What a person wrote is the truth; the derived
phrases are what give way.

One mechanical detail matters when a derived file is written while a process is
up. The registry fingerprints the `skill.md` files it discovers — path, size,
modification time — so writing a file beside one of them leaves that
fingerprint unchanged and would not by itself rebuild an index. The index folds
in a fingerprint of the derived files as well, which is what makes a newly
written file take effect without a restart.

## What the runtime says in its own words

Reaching the catalog is half of it. The other half is the answer. Most of a
reply is somebody else's text: a model's rephrasing, the catalog's wording, a
value a connector returned. The rest is the runtime speaking for itself: the
approval it asks for, the line that names a receipt, the sentence that says a
capability is down or that a write may or may not have landed. Those sentences
come from a phrase catalog, keyed by language and then by phrase, and are said
in the language the session settled on.

```json
{
  "en": {"approval_tail": "Reply {approve} to approve it, or {reject} to leave it alone."},
  "it": {"approval_tail": "Rispondi {approve} per approvarla, oppure {reject} per lasciare tutto com'è."}
}
```

The package ships the catalog in English and Italian; `reply.phrases` names the
file a deployment lays over it. The rules it is held to:

- **English is complete.** The set of phrases is fixed by the code that asks
  for them, and the packaged English holds every one. A configured file may
  translate a single phrase and leave the rest alone.
- **The fallback is per phrase.** A phrase missing in the language of the
  session is said in `reply.default_language`, and then in English. A partial
  translation is used for everything it covers.
- **Values are inserted verbatim.** An operation name, an integration id, a
  payload, a receipt id and a reason go into the sentence exactly as they are.
  A reason written by what the runtime talked to or checked (a policy
  explanation, the issues of a plan that did not hold up, a connector's error)
  is one of those values, so it keeps the language it was written in. Why the
  runtime itself dropped a request or held a write back is a phrase: the
  outcome names the phrase, and its record keeps the English reason for the
  audit trail.
- **A translation names the same placeholders.** A phrase whose placeholders
  differ from its English phrase is refused when the catalog is loaded, with
  the language, the phrase and both sets named, not when a turn first needs it.
- **The approval words are accepted words.** `approve_word` and `reject_word`
  are what an approval request tells a person to type. Each must be one of
  `router.rules.approve_tokens` or `router.rules.reject_tokens`, compared the
  way the rule layer compares them, or the catalog is refused: a reply never
  asks for an answer the router would not recognize. The check covers the
  languages the deployment answers in, `reply.default_language` and
  `discovery.languages`, not every language the catalog holds; a token list
  left empty is not checked, because the model router then reads the answer.
  The shipped Italian says `sì` and `no`.

Two entries are lists rather than sentences. `claim_verified` holds the words
that call a write verified and `negations` the phrases that say it is not. The
conformance check for an overstated write reads both, in every language the
catalog holds, so a reply in Italian is held to the same check as one in
English. They are matched as case-insensitive substrings, so a stem such as
`verificat` covers every ending.

What is handed to a model stays in English and out of the catalog: the facts
about whether a write landed, the instruction about a capability that is down,
the brief for a request outside the catalog, and the host facts. A model reads
them; a person does not.

Possible next steps, not taken yet: letting a pack contribute its own layer of
phrases, which the layered load already allows for, and translating the
catalog into further languages.

## Configuration

`reply.phrases` in [configuration.md](configuration.md#reply) names the phrase
catalog. `enrichment` and `discovery.languages` in
[configuration.md](configuration.md#enrichment). The short version:
`discovery.languages` says which languages the catalog is expected to be
reached in, `enrichment.enabled` says whether the runtime reads the derived
files, `enrichment.per_language` sets the size of a pass, and
`enrichment.cap_per_route` bounds the index.
