// dialer/scripts/cold_call_b2b.js
//
// PLACEHOLDER B2B cold-call script for the NovaIntel website-outreach dialer.
// Schema mirrors NovaIntel's ScriptNode. Replace prose freely; do not rename ids
// without updating any callers (live-coach gates, renderScript, autopsy).
//
// Variables filled in at render time:
//   {business_name}, {city}, {category}, {your_name}, {owner_name}

window.COLD_CALL_B2B = {

  start: {
    id: "start", badge: "Entry", type: "script",
    say: "Hey, is this the owner over at {business_name}?",
    direction: "Confirm decision-maker FAST. Don't introduce yourself yet — the longer you wait to be identified, the longer they listen. If it's not the owner, get the name and best time to call back.",
    tonality: "Relaxed, slightly casual. You're not nervous. Slight smile in your voice. You sound like someone who calls business owners every day because you do.",
    pacing: "Natural. Don't rush 'the owner over at {business_name}' — say the business name like it matters. Then stop.",
    warn: "Do NOT lead with 'My name is X with Y.' That's the telemarketer opener and they hang up in 2 seconds. ILLUSTRATION: When a friend calls a business, they don't say 'Hi, my name is Sarah Jenkins.' They say 'Hey, is John there?' Be that.",
    answers: [
      { label: "Yes — they're the owner",                 next: "state_reason" },
      { label: "Yes but they sound rushed / cold",        next: "pattern_interrupt" },
      { label: "Transferred to owner",                    next: "transferred" },
      { label: "No — it's a gatekeeper / receptionist",   next: "not_dm" },
    ],
  },

  not_dm: {
    id: "not_dm", badge: "Gatekeeper", type: "pivot",
    say: "Ah okay — no worries. Is {owner_name} around or is there a better time to catch them? Nothing urgent, just wanted to run something by them about the {business_name} website real quick.",
    direction: "Get the owner's name (if we don't have it) and a callback window. Don't pitch the gatekeeper. Stay casual — they're the gate, treat them like a colleague.",
    tonality: "Friendly, low-stakes. You're NOT trying to manipulate your way past them. You're being honest about wanting to talk to the owner.",
    pacing: "Calm. Don't oversell the 'nothing urgent.' If you say it twice it sounds suspicious.",
    warn: "DO NOT lie about the reason. Receptionists remember liars and warn the owner. ILLUSTRATION: 'I have an important matter for the owner' is the most common lie they hear daily. Don't be that caller.",
    answers: [
      { label: "Owner available — transferring",      next: "transferred" },
      { label: "Got callback window — log it",        next: "end_callback" },
      { label: "Gatekeeper refuses — exit politely",  next: "end_not_interested" },
    ],
  },

  transferred: {
    id: "transferred", badge: "Entry", type: "script",
    say: "Hey {owner_name}, thanks for picking up. Real quick — this is {your_name} with NovaIntel here in {city}. I know this is out of the blue. You have 30 seconds for me to tell you why I called?",
    direction: "Acknowledge the cold-call frame head-on. The '30 seconds + permission' ask is the highest-converting opener in the deck. Most owners say yes because you respected their time.",
    tonality: "Confident, not apologetic. You believe you're worth 30 seconds.",
    pacing: "Slow on '30 seconds for me to tell you why I called?' — that question needs to land. Pause and let them respond.",
    warn: "If they say no, do NOT push. Thank them and ask for a callback time. Hard sells at this point burn the lead. ILLUSTRATION: It's like asking someone for directions — if they say 'I'm busy' and you keep talking, you're now the rude stranger.",
    answers: [
      { label: "Yes, go ahead",                next: "state_reason" },
      { label: "What's this about? (curious)", next: "state_reason" },
      { label: "No, I'm busy",                 next: "obj_busy" },
      { label: "Not interested",               next: "obj_not_interested" },
    ],
  },

  pattern_interrupt: {
    id: "pattern_interrupt", badge: "Pivot", type: "pivot",
    say: "Yeah — I can hear you're in the middle of something. I'll be quick. I noticed something about {business_name}'s website I thought was worth a 30-second mention. Want the short version now or should I catch you later?",
    direction: "They're gruff because every cold call is bad. Match their energy — drop the friendliness, get to the point. Give them control of the timing.",
    tonality: "Crisp, direct. No filler. You match their cadence, not theirs match yours.",
    pacing: "Fast. Match their speed. The 'short version now or catch you later' is a power-of-choice close — both options serve you.",
    warn: "Do NOT try to charm them out of being short. ILLUSTRATION: You can't talk a stressed person into being calm. You can only respect the stress.",
    answers: [
      { label: "Short version now",      next: "state_reason" },
      { label: "Catch me later",         next: "end_callback" },
      { label: "Just not interested",    next: "obj_not_interested" },
    ],
  },

  state_reason: {
    id: "state_reason", badge: "Reason", type: "script",
    say: "Cool, appreciate it. So I help local {category} businesses turn their website into something that actually books calls instead of just sitting there. I pulled up yours before calling — saw some things I'd genuinely change if it was mine. Mind if I ask you two quick questions to see if there's any reason to keep talking?",
    direction: "Curiosity hook — you saw something specific. Not a pitch. The 'two quick questions to see if there's a reason to keep talking' is permission-based discovery. They almost always say yes.",
    tonality: "Casual confidence. You're qualifying THEM, not selling them. Slight 'I might leave you alone in 60 seconds' energy.",
    pacing: "Pause after 'just sitting there.' That's the curiosity gap. Don't fill it.",
    warn: "DO NOT describe what you saw on their website yet. The mystery is the hook. If you reveal early, the call dies. ILLUSTRATION: If a friend says 'I have to tell you something later' you'll think about it all day. Same mechanic.",
    answers: [
      { label: "Sure, ask away",                   next: "lead_source_q" },
      { label: "What did you see?",                next: "lead_source_q" },
      { label: "I already have a website",         next: "obj_have_website" },
      { label: "Just send me info",                next: "obj_send_info" },
      { label: "How much does it cost?",           next: "obj_price" },
    ],
  },

  lead_source_q: {
    id: "lead_source_q", badge: "Discovery", type: "script",
    say: "Awesome. First one — where do most of your new customers actually come from right now? Like is it word of mouth, Google, repeat business, somewhere else?",
    direction: "Open expansion question. Let them talk. Whatever they say, the next question goes deeper there. If they say 'word of mouth,' next q is about scaling. If 'Google,' next q is about cost per call.",
    tonality: "Genuinely curious. You don't know the answer. Their answer changes your follow-up.",
    pacing: "Ask, then SHUT UP. Count to 5 in your head. The first person to talk loses.",
    warn: "Do NOT jump in with assumptions or fill silence. ILLUSTRATION: A doctor doesn't suggest the diagnosis before you finish describing the symptom. Same posture.",
    answers: [
      { label: "Mostly word of mouth / referrals", next: "pain_q" },
      { label: "Google / search",                  next: "pain_q" },
      { label: "Repeat business",                  next: "pain_q" },
      { label: "Honestly not sure",                next: "pain_q" },
      { label: "Pushed back / wants to know more first", next: "value_anchor" },
    ],
  },

  pain_q: {
    id: "pain_q", badge: "Discovery", type: "script",
    say: "Gotcha. And what would you change if you could wave a magic wand at the way new customers find {business_name} today? Like what's actually frustrating about it?",
    direction: "The 'magic wand' frame disarms the corporate filter. They tell you the real problem instead of the polite one. This is where the consequence hook lives.",
    tonality: "Soft, curious. You sound like a friend asking, not a salesperson probing.",
    pacing: "Slow on 'frustrating.' That word legitimizes their complaints.",
    warn: "Do NOT immediately tie their pain to your solution. Let the pain breathe first. ILLUSTRATION: When someone tells you they have a headache, you don't immediately pull out Tylenol. You ask how long it's been going on.",
    answers: [
      { label: "They named a real pain",            next: "consequence_q" },
      { label: "Says nothing's really wrong",       next: "value_anchor" },
      { label: "Deflects / 'send me something'",    next: "obj_send_info" },
    ],
  },

  consequence_q: {
    id: "consequence_q", badge: "Consequence", type: "script",
    say: "That makes sense. And if that stayed exactly the same for the next 12 months — same number of calls coming in, same kind of customers — what does that actually mean for the business? Like in real terms.",
    direction: "Anti-resistant consequence question. You're not pitching urgency — they're naming it themselves. Whatever number they say, that's their motivation, not yours.",
    tonality: "Quiet. Almost gentle. You are NOT applying pressure. You're asking them to look at the math.",
    pacing: "Very slow on 'what does that actually mean.' Then count to 7. This silence is the most expensive in the call — it forces the consequence to land.",
    warn: "If you fill this silence, you blew the whole call. ILLUSTRATION: Doctors who tell you 'this could be serious' and immediately pivot to treatment options lose your trust. Let the weight sit.",
    answers: [
      { label: "They named a financial consequence",  next: "value_anchor" },
      { label: "They named an emotional one",         next: "value_anchor" },
      { label: "Shrugs / 'I dunno, same as now'",     next: "value_anchor" },
    ],
  },

  value_anchor: {
    id: "value_anchor", badge: "Value", type: "script",
    say: "Yeah, that's exactly what I see with a lot of {category} owners. What I do is pretty simple — I build the site as a preview FIRST, you see exactly what it would look like for {business_name}, and if you like it we go from there. No 30-page proposal, no $5K upfront, no agency runaround. Want me to put a preview together so you can just react to it?",
    direction: "Frame the offer as zero-commitment. The 'preview-first' is the differentiator — they can react to something concrete instead of buying a promise.",
    tonality: "Casual. This is the easiest yes in the call — you're offering free work.",
    pacing: "Slightly faster here. You're sounding eager to build, not eager to close.",
    warn: "DO NOT mention price unless asked. The preview is the close. ILLUSTRATION: A restaurant doesn't quote you a bill before you've seen the food. Sample first, price later.",
    answers: [
      { label: "Sure — put a preview together",     next: "book_discovery" },
      { label: "How much would the real thing cost?", next: "obj_price" },
      { label: "Need to think about it",            next: "obj_think_about_it" },
      { label: "Send me info first",                next: "email_capture" },
    ],
  },

  obj_have_website: {
    id: "obj_have_website", badge: "Objection", type: "pivot",
    say: "Yeah, I figured — most {category} businesses do. The real question is is it actually bringing you new customers, or is it just a brochure that sits there because you needed one? Genuinely curious which it is for you.",
    direction: "Reframe 'have one' vs 'works.' Most owners have a site that does nothing — they just don't think about it because it was a one-time expense years ago.",
    tonality: "Not combative. 'Genuinely curious which it is for you' makes it a real question, not a setup.",
    pacing: "Pause on 'just a brochure that sits there.' That metaphor is the hook.",
    warn: "Do NOT trash their current site outright. ILLUSTRATION: Insulting their existing site is insulting their past decision. Frame it as a tool that may have served its time, not a bad choice.",
    answers: [
      { label: "It actually works — bringing customers",  next: "obj_have_website_works" },
      { label: "Honestly it's just sitting there",        next: "lead_source_q" },
      { label: "Not sure",                                next: "lead_source_q" },
    ],
  },

  obj_have_website_works: {
    id: "obj_have_website_works", badge: "Objection", type: "pivot",
    say: "Hell yeah, that's great to hear — most don't. Out of curiosity, when's the last time you actually counted how many calls came in from it versus other sources? Because most owners I talk to think it's working until we run the numbers and it turns out 80% of leads are coming from one source they didn't know about.",
    direction: "Doubt-seed. You're not arguing — you're suggesting the data might tell a different story. Most owners haven't actually measured.",
    tonality: "Curious, almost conspiratorial — 'I'll let you in on a secret most owners miss.'",
    pacing: "Fast on 'we run the numbers,' slow on '80% of leads.' The number is the hook.",
    warn: "DO NOT claim you'll fix something they think isn't broken. ILLUSTRATION: A mechanic who says 'your car runs fine but let me change everything' is a thief. Ask the question, let them discover.",
    answers: [
      { label: "Actually I don't track it",       next: "lead_source_q" },
      { label: "Fair point — I should check",     next: "value_anchor" },
      { label: "I'm confident in our setup",      next: "end_not_interested" },
    ],
  },

  obj_send_info: {
    id: "obj_send_info", badge: "Objection", type: "pivot",
    say: "Yeah I can definitely send something over. Just so I'm not sending you a generic deck that goes straight to trash — what would actually be useful for you to see? Like a couple examples of {category} sites we've done, or pricing, or something more specific to {business_name}?",
    direction: "'Send me info' is a smoke-screen 90% of the time. Strip it — ask what they ACTUALLY want. If they can't say, the real objection comes out.",
    tonality: "Helpful and clarifying. You're trying to send the RIGHT thing, not arguing about whether to send.",
    pacing: "Normal. Don't sound suspicious of the request.",
    warn: "DO NOT just send a generic info packet. ILLUSTRATION: It's like a doctor sending a generic 'here's some health tips' instead of asking what hurts. They throw it away.",
    answers: [
      { label: "Specific examples for my industry",     next: "email_capture" },
      { label: "Just generic info / they can't say",    next: "obj_send_info_strip" },
      { label: "Honestly I just want you off the phone", next: "obj_not_interested" },
    ],
  },

  obj_send_info_strip: {
    id: "obj_send_info_strip", badge: "Objection", type: "pivot",
    say: "Got it — totally fair. Real talk though: I can save us both 20 minutes if you tell me what's actually making you want to push this off. Is it timing, money, or you're just not sure the website is the right thing to focus on right now?",
    direction: "Strip the smoke-screen to bone. Naming the three real objections lets them pick one and tells you exactly what to handle.",
    tonality: "Honest, almost vulnerable. You're dropping the sales pretense. 'Real talk' signals authenticity.",
    pacing: "Slow on each option — 'timing,' pause, 'money,' pause, 'not sure the website is right.' Each pause lets them weigh which is theirs.",
    warn: "DO NOT skip any of the three. ILLUSTRATION: A multiple-choice question with 2 options feels like a trap. 3 feels like a real choice.",
    answers: [
      { label: "Timing — busy season / not now",    next: "end_callback" },
      { label: "Money — out of budget",             next: "obj_price" },
      { label: "Not sure it's the right priority",  next: "consequence_q" },
      { label: "Just done with the call",           next: "end_not_interested" },
    ],
  },

  obj_busy: {
    id: "obj_busy", badge: "Objection", type: "pivot",
    say: "Yeah I figured you might be — running a {category} shop isn't exactly a sit-around job. When's the actual best time to catch you for 10 minutes? End of day? Lunch?",
    direction: "Don't fight the busy. Schedule into it. Naming specific windows (end of day, lunch) is easier to say yes to than 'when's good?'",
    tonality: "Empathetic, no pressure. You sound like someone who respects their schedule.",
    pacing: "Quick. Don't make them feel like they're being pressured to schedule.",
    warn: "DO NOT say 'just 5 minutes.' Everyone knows it's never just 5. ILLUSTRATION: '10 minutes' is honest. '5' is a lie they've heard 100 times.",
    answers: [
      { label: "Got callback window",       next: "end_callback" },
      { label: "Just send info instead",    next: "obj_send_info" },
      { label: "Still no — hard pass",      next: "end_not_interested" },
    ],
  },

  obj_not_interested: {
    id: "obj_not_interested", badge: "Objection", type: "pivot",
    say: "Totally fair — and honestly thanks for being straight about it. Most people would have made me work for that no. Quick last thing before I let you go: is it that you're not interested in talking to me, or not interested in growing the customer base for {business_name} this year? Because if it's the second one I'll never call you again.",
    direction: "Reframe the no. 'Not interested' is almost always 'not interested in YOU right now' — not the whole topic. Separating the two reveals which it really is.",
    tonality: "Light, almost amused. You appreciate the honesty. Zero defensiveness.",
    pacing: "Slow on 'is it that you're not interested in talking to me.' The pause separates the two options.",
    warn: "DO NOT use this if they were hostile. Only works on reflexive nos. ILLUSTRATION: A flexible no opens the door. A hard no slams it. Read the energy first.",
    answers: [
      { label: "Actually I do want to grow — what did you see?", next: "state_reason" },
      { label: "Genuinely not interested in either",             next: "end_not_interested" },
      { label: "DNC me",                                         next: "end_dnc" },
    ],
  },

  obj_price: {
    id: "obj_price", badge: "Objection", type: "pivot",
    say: "Fair question. Honest answer: I don't quote a price until you've seen the actual preview, because the price depends on what you want it to do. Some {category} sites I build are $1,200 and some are $4,500. The preview's free either way — that's how you decide if it's worth a number. Cool?",
    direction: "Anchor a range, defer the exact number to after the preview. The free preview is the close — price is the future.",
    tonality: "Matter-of-fact. You're not dodging the question — you're explaining why a real number requires real info.",
    pacing: "Normal. Don't rush past the range. Both numbers should land.",
    warn: "DO NOT quote a single price upfront. Anchoring high then negotiating down is amateur. ILLUSTRATION: A custom suit tailor doesn't quote you a price before measuring you. Same here.",
    answers: [
      { label: "Sure — build the preview",       next: "book_discovery" },
      { label: "Still too much",                 next: "end_not_interested" },
      { label: "Need to think about it",         next: "obj_think_about_it" },
    ],
  },

  obj_think_about_it: {
    id: "obj_think_about_it", badge: "Objection", type: "pivot",
    say: "Yeah I get it — and look, you don't need to think about the preview because it's free and zero-commitment. What I'd actually want you to think about is whether the way customers find {business_name} today is good enough to keep doing for another year. If yes, we don't need to talk again. If not, the preview tells you whether we're the right fix. Want me to just build it and send it over?",
    direction: "'Think about it' is the second-most common smoke-screen. Reframe what they need to think about — the preview is free, so the real decision is whether the status quo is good enough.",
    tonality: "Soft challenge. You're respecting the no, but pointing out the real question.",
    pacing: "Slow on 'whether the way customers find {business_name} today is good enough to keep doing for another year.' That's the consequence question disguised as a permission ask.",
    warn: "DO NOT push for a decision now. ILLUSTRATION: 'Just say yes today' is sales 101 and they've heard it 50 times. Make them want to say yes when you're not on the phone.",
    answers: [
      { label: "Yeah, build it and send",         next: "book_discovery" },
      { label: "I'll think about it for real",    next: "end_callback" },
      { label: "Pass — not for me",               next: "end_not_interested" },
    ],
  },

  book_discovery: {
    id: "book_discovery", badge: "Close", type: "script",
    say: "Awesome. I'll have the preview ready in about 48 hours. What's the best email to send the link to? And I'll text you when it's live so you can pull it up on your phone.",
    direction: "Two micro-commitments at once — email + phone-text permission. Both feel like logistics, not commitments.",
    tonality: "Already-done energy. You're not asking permission to build — you're collecting logistics for something that's happening.",
    pacing: "Fast. This isn't a negotiation. You're confirming details for a project that's now in motion.",
    warn: "DO NOT pause for them to reconsider. ILLUSTRATION: A restaurant taking your order doesn't say 'are you SURE you want the steak?' They write it down.",
    answers: [
      { label: "Got email and phone — done",       next: "end_interested" },
      { label: "They want to think about it now",  next: "obj_think_about_it" },
    ],
  },

  email_capture: {
    id: "email_capture", badge: "Email", type: "script",
    say: "Perfect. Best email to send it to? I'll send a couple {category} examples and a one-page on what we do — quick read, no fluff.",
    direction: "Lower-commitment exit. They got something, you got the email, you have a reason to follow up.",
    tonality: "Clean and quick. Logistics tone.",
    pacing: "Fast. Don't linger.",
    warn: "DO NOT send a 12-page PDF. ILLUSTRATION: Quick read = they read it. Long deck = trash.",
    answers: [
      { label: "Got email",          next: "end_interested" },
      { label: "Changed mind / nah", next: "end_not_interested" },
    ],
  },

  // ── TERMINAL EXITS — wired to outcome buttons ──────────────────────────────
  end_interested:     { id: "end_interested",     badge: "Exit", type: "exit", outcome: "interested" },
  end_callback:       { id: "end_callback",       badge: "Exit", type: "exit", outcome: "callback" },
  end_not_interested: { id: "end_not_interested", badge: "Exit", type: "exit", outcome: "not_interested" },
  end_dnc:            { id: "end_dnc",            badge: "Exit", type: "exit", outcome: "dnc" },
};
