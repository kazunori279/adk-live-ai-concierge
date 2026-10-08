# Structural patterns

The full catalog with examples. Load this when SKILL.md's summary is not enough: a thorough review, a contested edit, or when the writer wants to see the fix demonstrated.

Any one of these used once can be fine. The problem is several appearing together, or one repeated.

## Sentence structure

### Binary contrasts (negative parallelism)

The most commonly identified AI tell. Frames everything as a surprising reframe. Before LLMs, nobody wrote like this at scale.

| Pattern | Problem |
|---|---|
| "Not because X. Because Y." | Telegraphed reversal |
| "X isn't the problem. Y is." | Formulaic reframe |
| "The answer isn't X. It's Y." | Predictable pivot |
| "It feels like X. It's actually Y." | Setup/reveal cliché |
| "The question isn't X. It's Y." | Rhetorical misdirection |
| "It's not just X but Y." / "not only X but also Y" | Additive hedge |
| "stops being X and starts being Y" | False transformation arc |

Fix: state Y directly. Drop the negation.

Before: "It's not just about the beat riding under the vocals; it's part of the aggression and atmosphere. It's not merely a song, it's a statement."
After: "The heavy beat adds to the aggressive tone."

The tailing variant is the same tell compressed: "The options come from the selected item, no guessing." Write the clause: "...without forcing the user to guess."

### Negative listing

"Not a X. Not a Y. A Z." A rhetorical striptease.

Before: "Not ten. Not fifty. Five hundred and twenty-three lint violations across 67 files."
After: "523 lint violations across 67 files."

### Self-posed rhetorical questions

The model asks a question nobody asked, then answers it.

"The result? Devastating." / "The worst part? Nobody saw it coming." / "What if [reframe]?" / "Here's what I mean:" / "Think about it:" / "And that's okay."

Fix: make the point.

### Anaphora abuse

"They assume that... They assume that... They assume that..."
"They have built engines, but not vehicles. They have built power, but not leverage."

Fix: vary openings, or merge the points into one sentence. Deliberate repetition for rhythm ("She came. She saw. She conquered.") is voice, not slop.

### Tricolon abuse

A single tricolon is elegant. Three back-to-back is a pattern-recognition failure.

"Products impress; platforms empower. Products solve; platforms create. Products scale linearly; platforms scale exponentially."

Fix: use the natural number. Two often beats three.

Before: "Attendees can expect innovation, inspiration, and industry insights."
After: "The event includes talks and panels, with time to meet people between sessions."

### False ranges

"From X to Y" implies a spectrum with a meaningful middle. Models use it to list two loosely related things.

Before: "Our journey has taken us from the singularity of the Big Bang to the grand cosmic web, from the birth and death of stars to the enigmatic dance of dark matter."
After: "The book covers the Big Bang, star formation, and current theories about dark matter."

### Superficial participle tails

An "-ing" phrase bolted onto a sentence end to inject depth that isn't there.

"contributing to the region's rich cultural heritage" / "highlighting its enduring legacy" / "underscoring its role as a dynamic hub" / "reflecting broader trends in..."

Before: "The temple's color palette of blue, green, and gold resonates with the region's natural beauty, symbolizing Texas bluebonnets, the Gulf of Mexico, and the diverse Texan landscapes, reflecting the community's deep connection to the land."
After: "The temple is painted blue, green, and gold, colors meant to evoke Texas bluebonnets and the Gulf of Mexico."

### False agency

Inanimate things doing human verbs. Models like it because it avoids naming the actor.

| Pattern | Who actually acts |
|---|---|
| "a complaint becomes a fix" | Someone fixed it |
| "the decision emerges" | Someone decides |
| "the culture shifts" | People change behavior |
| "the conversation moves toward" | Someone steers |
| "the data tells us" | Someone read it and concluded |
| "the market rewards" | Buyers pay for things |

Fix: name the human. If no specific person fits, use "you."

### Passive and subjectless constructions

"X was created" / "It is believed that" / "Mistakes were made" / "The decision was reached" / "No configuration file needed." / "The results are preserved automatically."

Fix: find the actor and put them at the front. Passive is fine when the actor is unknown or genuinely does not matter.

Before: "No configuration file needed. The results are preserved automatically."
After: "You don't need a configuration file. The system preserves the results automatically."

### Narrator-from-a-distance and Wh-opener crutch

"Nobody designed this." / "People tend to..." / "This happens because..." / "One might say..."
Paragraphs opening with What / When / Where / Which / Who / Why / How, or with "So,".

Before: "What makes this hard is the coordination cost. People tend to underestimate it."
After: "The coordination cost is the hard part. You notice it the first time two teams ship the same fix."

### Synonym cycling and elegant variation

Repetition penalties make models rename the same thing every sentence. Humans repeat the clearest word. This happens at the word level and at the noun-phrase level ("the artist," "the visionary creator," "the non-conformist painter").

Before: "The protagonist faces many challenges. The main character must overcome obstacles. The central figure eventually triumphs. The hero returns home."
After: "The protagonist faces many challenges but eventually triumphs and returns home."

Fix the repeated *pattern*, not the repeated word. The remaining sentence may still start with "She."

### Lazy extremes

every, always, never, everyone, everybody, nobody doing vague work.

Before: "Everyone struggles with alignment. Nobody wants to admit confusion."
After: "Most teams I've worked with struggle with alignment, and few people admit it."

### Invented concept labels

Abstract problem-nouns (paradox, trap, creep, divide, vacuum, inversion) bolted to domain words and used as if they were established terms: "the supervision paradox," "the acceleration trap," "workload creep." They name a thing to skip the argument.

Fix: define the term if it needs a name. Otherwise describe it plainly.

### Answering objections nobody raised

"This isn't mainly about X" / "I'm not saying" / "To be clear" / "Don't get me wrong" / "You could argue this differently, but".

And the related tell, rejecting a fake alternative: "A tempting approach would be..." / "One might be tempted to..." / "It would be easy to just...", introducing an option no reader would consider, dismissing it, and never mentioning it again. Usually a leftover drafting thought.

Before: "Session tokens are rotated every 24 hours. A tempting approach would be to rotate them by restarting the auth service on a cron job, but that would drop every active session. Rotation happens in place, and clients refresh transparently."
After: "Session tokens are rotated every 24 hours, in place, and clients refresh transparently."

One rejected option may be genuine. Several short unrelated rejections is the tell.

## Paragraph and document structure

### Dramatic fragmentation

RLHF pushed models toward one thought per sentence, no mental state-keeping required. No human writes first drafts this way.

"[Noun]. That's it. That's the [thing]." / "X. And Y. And Z." / "He published this. Openly. In a book." / an orphaned fragment as a punchline.

Before: "Then AlphaEvolve arrived. It had no preference for symmetry. No aesthetic prior. No nostalgia for human taste. The old rules were gone."
After: "AlphaEvolve changed the search because it did not favor symmetry or human-looking designs. That made some of the older assumptions less useful."

One short sentence for emphasis is fine. A run of them is engineered.

### Listicle in a trench coat

A list wrapped in paragraphs to disguise the format: "The first wall is... The second wall is... The third wall is..."

Fix: if it is a list, make it a list. If it should be prose, weave the points together without numbering.

### Fractal summaries

"What I'm going to tell you; what I'm telling you; what I just told you," applied at every level. Every subsection gets a summary, every section gets a summary, the document gets a summary.

Fix: cut the frames. Let the writing move.

### One-point dilution and the treadmill effect

One argument restated ten ways with different metaphors and framings, so 800 words become 4,000. The mid-paragraph tells are "In other words," "Put simply," "Essentially," "That is to say."

Fix: apply the "what's actually new here?" test per sentence. Delete rephrasings.

### The dead metaphor

A human introduces a metaphor, uses it, and moves on. Models repeat the same one 5-10 times: walls and doors 30 times in one article, "primitives" in every paragraph.

Fix: use it once or twice, then drop it.

### Historical analogy stacking

Rapid-fire name-drops for false authority. Especially common in technical writing.

"Apple didn't build Uber. Facebook didn't build Spotify. Stripe didn't build Shopify."
"Every major shift, the web, mobile, social, cloud, followed the same pattern."

Fix: one example examined in depth beats five name-drops.

### "Despite these challenges..."

Problems acknowledged only to be dismissed, always on the same beat.

Before: "Despite its industrial prosperity, Korattur faces challenges typical of urban areas, including traffic congestion and water scarcity. Despite these challenges, with its strategic location and ongoing initiatives, Korattur continues to thrive as an integral part of Chennai's growth."
After: "Korattur has recurring traffic congestion and water shortages."

### Paragraph-reshuffling immunity

Can you swap paragraphs 2 and 4 without breaking anything? Then the piece is a stack of parallel self-contained mini-theses, not an unfolding argument.

Fix: make each paragraph depend on the last. Merge or cut the interchangeable ones.

### Fragmented headers

A heading followed by one line that restates it.

Before:
> ## Performance
>
> Speed matters.
>
> When users hit a slow page, they leave.

After:
> ## Performance
>
> When users hit a slow page, they leave.

### Sudden register shift

An AI-written section carries a different voice and error profile than the human parts: formal clean English beside casual text with typos, or spelling conventions that switch mid-piece.

Fix: hold one register.

## Content patterns

### Significance inflation

Ordinary details claimed to mark a major change or reflect a broad trend.

Before: "The Statistical Institute of Catalonia was officially established in 1989, marking a pivotal moment in the evolution of regional statistics in Spain. This initiative was part of a broader movement across Spain to decentralize administrative functions and enhance regional governance."
After: "The Statistical Institute of Catalonia was established in 1989, part of a wider decentralization of administrative functions in Spain."

### Symbolic gloss

Narrating the meaning of a fact instead of trusting it: "represents," "symbolizes," "speaks to," "embodies," "reflects broader," applied to mundane things.

Fix: state the fact and let the reader interpret.

### Promotional language

Especially for places, culture, products, and organizations.

Before: "Nestled within the breathtaking region of Gonder in Ethiopia, Alamata Raya Kobo stands as a vibrant town with a rich cultural heritage and stunning natural beauty."
After: "Alamata Raya Kobo is a town in the Gonder region of Ethiopia."

### Name-dropping and source-listing as content

Before: "Her views have been cited in The New York Times, BBC, Financial Times, and The Hindu. She maintains an active social media presence with over 500,000 followers."
After: "Her views have been cited in The New York Times and the BBC."

Keep a citation when the source says what the person actually said and where. Never invent context to justify keeping it.

### Diff-anchored writing

Docs and comments narrating a change instead of describing current behavior. Unless the document is version-scoped (changelog, release notes, migration guide), it should read coherently without knowing what changed last commit.

Before: "This function was added to replace the previous approach of iterating through all items, which caused O(n²) performance."
After: "This function uses a hash map for O(1) lookups, avoiding the O(n²) cost of naive iteration."

Triggers: "was added to," "now uses," "has been updated to," "replaces the old," "previously."

## Statistical tells

Beyond individual patterns, check the shape of the text.

| Signal | Human | AI | Why |
|---|---|---|---|
| Burstiness (sentence-length variance) | High | Low | Humans write in bursts: short, then long. Models are metronomic. |
| Type-token ratio | 0.5-0.7 | 0.3-0.5 | Models cycle the same words. |
| Trigram repetition | Low | High | Models reuse the same three-word phrases. |
| Paragraph uniformity | Varied | Even | Model paragraphs are all roughly the same size. |

Fix these directly: vary sentence lengths, vary paragraph sizes, repeat the clearest word instead of cycling synonyms.

**Burstiness target.** Mix short (3-8 words), medium (12-20), and long (25-40) in every paragraph. Never three consecutive sentences of similar length. But do not chain fragments either: connect related thoughts with subordinate clauses, conjunctions, semicolons, and commas so the syntax shows causation, contrast, and qualification.

**Perplexity.** Models pick the highest-probability next word. Reach past the first word that comes to mind. Domain-specific vocabulary, an unexpected analogy from real experience, and an informal transition all raise it naturally.

## Voice profiles

Infer from the input, or ask.

- **Casual.** Contractions always. First person where it fits. Informal transitions. Occasional parenthetical asides. Fragments for emphasis. "And" and "But" openers allowed.
- **Professional.** Selective contractions. Third person by default, first person for opinions. Dry wit over jokes. Concrete examples. Short paragraphs.
- **Technical.** Precise vocabulary, the exact term over a simpler one. One point per sentence. Concrete numbers over vague quantities. No metaphors unless they genuinely clarify. Neutral is correct here; do not inject personality.
- **Warm.** Contractions always. "We" and "our." Acknowledge difficulty. Encouragement without sycophancy. More whitespace.
- **Blunt.** Shortest possible sentences. No hedging. Strong opinions stated as facts. No pleasantries. Active voice only.

## Soul techniques

Only where the genre calls for it. On reference, technical, legal, or factual text, plain and neutral *is* the human voice.

1. Have an opinion. React instead of reporting. "This API design is frustrating" beats "The API has certain limitations."
2. Calibrate certainty on a spectrum. High conviction: "clearly," "no question." Medium: "I think," "in my experience." Real doubt: "I'm not sure, but." A real mind moves across that range; models park at flat medium. Never stack hedges.
3. Use specific experiential detail: "debugging this at 2am with a stack trace that makes no sense," not "the process is complex."
4. Allow a tangent or an aside. It signals a thinking mind.
5. Vary paragraph length dramatically. Four sentences, then one line.
6. Break parallel structure occasionally. Three items with the same grammar, then a fourth that differs.
7. Use callbacks. "Remember that API I called frustrating? It gets worse."
8. Self-correct. "The system handles auth, well, authentication and authorization are separate, but you get the idea."
9. End without wrapping up. Not every piece needs a neat conclusion.

Never add a factual claim to manufacture any of this.

## Scoring (optional)

When the user asks how bad a draft is, rate 1-10 on each dimension. Below 35/50, revise. Do not score during a detect pass; name patterns instead.

| Dimension | Question |
|---|---|
| Directness | Statements or announcements? |
| Rhythm | Varied or metronomic? |
| Trust | Respects the reader's intelligence? |
| Authenticity | Sounds like a specific person wrote it? |
| Density | Anything cuttable? |
