# No AI slop eval

Use this after the rewrite. Answer each check with pass or fail. If any check fails, fix the draft before returning it.

For detect requests, run only the *Detect output* section at the end.

## Voice and restraint

1. Does the edit preserve the writer's distinctive vocabulary, cadence, bluntness, humor, uncertainty, digressions, and level of polish?
2. Does it leave strong human sentences alone instead of rewriting them for consistency or making every paragraph equally tidy?
3. Is the amount of cutting proportional to the actual slop, with no aggressive compression that strips out character?
4. Was every flagged candidate validated (does cutting lose meaning? formula or choice? announces or delivers? heat or light? more trust or less?) before it was cut?
5. If the user supplied a writing sample, does the rewrite match its sentence lengths, punctuation habits, and recurring phrases, overriding this skill's style rules where they conflict?
6. Was the register kept where it started, rather than neutralized toward a generic middle?
7. Would the writer recognize the edited draft as their own voice?

## Fabrication

1. Does the rewrite add no fact, name, number, date, quote, citation, or anecdote that is not in the source or from the user?
2. Is every claim from the original still present, or deliberately cut with the user informed?
3. Where the source offered only a vague benefit, does the rewrite name the missing mechanism, owner, metric, or evidence instead of restating the benefit as true?
4. Are hypotheticals marked as hypothetical?
5. Are instructions about the writing (notes addressed to the editor) absent from the output?

## Words

1. Are Tier 1 words and filler phrases removed unless quoted as examples?
2. Are Tier 2 words cut where they cluster, and Tier 3 words left alone unless they stand in for a specific fact?
3. Are often-empty adverbs cut where they add nothing and kept where they carry emphasis, uncertainty, contrast, or spoken rhythm?
4. Are copula dodges ("serves as," "boasts," "features") replaced with *is* and *has*?
5. Are business jargon and abstract metaphor nouns replaced with the concrete word?

## Patterns

1. Are binary contrasts, negative listings, rhetorical setups, and throat-clearing openers removed?
2. Are faux-insight setups, colon reveals, superficial -ing tails, synonym cycling, dramatic fragments, and robotic rhythm fixed?
3. Are importance puffery, vague declaratives, lazy extremes, and grandiose stakes replaced with the specific fact?
4. Is weasel attribution replaced with a named source, or flagged for the user when no source exists?
5. Is false agency replaced with a human subject, and is narrator-from-a-distance put back in the room?
6. Are Wh-opener crutches restructured to lead with the subject?
7. Is interpretive metadiscourse removed, including authorial metacommentary, reader guidance, emphasis markers, and redundant glossing?
8. Are invented concept labels either defined or described plainly?
9. Are aphorism formulas and pull-quote lines rewritten as the concrete claim?
10. Is one-point dilution cut, and is any repeated metaphor used at most twice?
11. Are fake objections and rejected fake alternatives removed?
12. Are chatbot artifacts, reasoning-chain leftovers, acknowledgment loops, cutoff disclaimers, and speculative gap-fill gone?
13. Are hedge stacks reduced to one qualifier per claim?
14. Are fake-profound kicker lines deleted instead of rewritten into better metaphors?
15. Are summary-recap endings and generic positive endings cut, so the piece ends on a concrete point, takeaway, or next action?

## Rhythm and shape

1. Does the draft avoid three consecutive sentences of similar length?
2. Does it avoid chained fragments, with related thoughts connected by clauses, conjunctions, or semicolons?
3. Do paragraph lengths vary?
4. Does each paragraph depend on the previous one, so paragraphs 2 and 4 could not be swapped without breaking the argument?
5. Does the draft avoid robotic symmetry, repeated sentence shapes, and every paragraph ending punchy?

## Formatting and punctuation

1. Are em and en dashes absent from short copy, and at most one per 500 words in a longer draft, unless the writer's sample uses them? Was the final text searched for `—`, `–`, and ` -- `?
2. Are headings sentence case, with no emoji and no decorative bold?
3. Are bold-first bullets that restate their own label converted to prose?
4. Are bullets used only for genuinely parallel items, with prose where two sentences read better?
5. Is markdown absent from plain-text contexts (email, DM, SMS, social)?
6. Are straight quotes and plain arrows used, with no zero-width characters or homoglyphs?
7. Are forensic artifacts stripped: citation markup, AI UTM parameters, placeholder brackets?
8. Does the structure follow the content, with no headers over two-sentence sections?

## Soul

1. If the genre calls for voice, does the draft have a real position, calibrated certainty, and at least one concrete experiential detail rather than clean neutrality?
2. If the text is reference, technical, legal, or factual, was personality kept out?
3. Was no factual claim invented to manufacture personality?

## False positives

1. Was nothing flagged solely for polish, formal vocabulary, one transition word, curly quotes, a lone em dash, one short emphatic sentence, or a missing citation?
2. Were watched phrases inside quotations, titles, proper names, and examples left untouched?
3. Were genuine scope, legal, and safety caveats, and real alternatives in a design doc, kept?
4. Was every remaining tell that survived validation reported to the user as a deliberate keep, with the reason?

## Technical documentation

Run these only when the draft is a README, docs page, comment, API doc, or design doc.

1. Is every remaining statement still true of the current code, with stale and redundant content cut rather than left in place?
2. Was content that is wrong or obsolete deleted, instead of hedged or marked "TODO update"?
3. Does the doc avoid restating a guide that already exists elsewhere, linking to it instead?
4. Is each piece of information at the right level: name, inline comment, API doc, README, docs directory, or design doc?
5. Do comments explain why, while API docs state the contract: arguments, return values, gotchas, errors?
6. For a README, does it say what the directory holds, which files to read first, and who maintains it?
7. If usage examples are included, is the simplest case listed first?
8. Does the doc describe current behavior rather than narrating what changed, unless it is a changelog or migration guide?
9. Is domain terminology left intact, with only business buzzwords and AI vocabulary removed?
10. Is the doc short enough that a reader will actually keep it up to date?

## Final read

1. Was the edit checked directly against this file without requiring separate editor and evaluator agents?
2. Was the draft asked "what still makes this look AI-generated?" and the answer acted on?
3. Does the rewrite avoid replacing old slop with new formula: clipped aphorisms, tidy triads, forced contrast, generic consultant voice?
4. Is this skill's own checklist shape absent from the output?
5. Would the edited draft sound natural if read to a sharp colleague?
6. Does the final output include the full edited draft and a short **What changed** section naming the pattern behind each edit?

## Detect output

Run these instead of the sections above when the request was detect-only.

1. Does the response name each pattern found, quote the exact span, and give the fix in a few words?
2. Does it avoid rewriting the draft, scoring it, or claiming AI authorship?
3. Does it mark preserved spans and say why, where that helps?
4. Does it offer to edit the draft afterwards?
