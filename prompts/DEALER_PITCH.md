# Dealerly — Dealer Pitch Guide

## What You Are Selling

Dealerly is a private tool that finds genuinely underpriced used cars on eBay UK and Facebook Marketplace in real time. It scores each listing against live AutoTrader comps, checks MOT history, estimates repair costs, and produces a single ranked HTML report with a max-bid figure for every car.

Dealers would pay a subscription fee (£X/month or per-seat) to access runs tailored to their stock profile (price band, radius, preferred makes).

---

## Target Buyer Profile

| Profile | Why they care |
|---|---|
| Independent used-car dealer (2–10 cars on the lot) | Buys stock weekly; already using eBay; time-poor |
| Part-time car flipper (1–3 cars/month) | Lacks systematic sourcing; scared of overpaying |
| Bodyshop / mechanic who resells | Wants "profit after repair" number, not just asking price |

**Ideal first test**: a dealer with £3k–£15k working capital buying sub-£3k cars.

---

## Cold Email Template

**Subject line options (A/B test):**
- `Found you 3 eBay cars with £150+ profit this morning — want the report?`
- `Automated stock sourcing for [Dealership Name] — 2-minute demo?`
- `I built a tool that does your eBay sourcing — here's today's output`

**Body:**

```
Hi [Name],

I built a tool called Dealerly that automatically scans eBay UK and
Facebook Marketplace every morning, scores each listing against live
AutoTrader prices, and outputs a ranked HTML report with a recommended
max bid for each car.

This morning it flagged [X] cars in the £[price range] bracket within
[Y] miles of [location] with an estimated profit of £[range] after typical
prep costs.

I'm looking for a handful of dealers to trial it — happy to send you
today's report for free so you can judge for yourself whether the picks
are realistic.

Would a 2-minute look be worth it?

[Your name]
[Phone / WhatsApp]
```

**Follow-up (3 days later, if no reply):**

```
Hi [Name], just circling back — I sent across some sourcing data for
cars near you. Happy to walk you through it on a call or just forward the
latest report if easier. No obligation.
```

---

## In-Person Pitch

### Opening (30 seconds)

> "I've built a sourcing tool that scans eBay and Facebook every day and
> gives you a shortlist of the best underpriced cars — with the AutoTrader
> value, estimated repair cost, and a max bid pre-calculated. Takes about
> 10 minutes to run. Want to see what it found this morning?"

### Show the Report (2–3 minutes)

Open the HTML report on your laptop/phone. Walk through:

1. **BUY cards** (green border) — "These are the ones worth chasing today. Here's the max bid I'd offer, here's the estimated profit, and here's the MOT history."
2. **OFFER cards** (amber border) — "These are close — room to negotiate."
3. **Thumbnail + eBay link** — "Click this and you're straight on the listing."
4. **Repair estimate** — "It factors in typical prep: i-CTDI injectors, timing chains, EGR — whatever shows in the MOT history."

### Key Points to Land

- **It does the spreadsheet work for you.** No manual cross-referencing on AutoTrader.
- **MOT history is already checked.** DVSA data, not just the seller's word.
- **You set the profit bar.** The tool only shows cars that clear your margin.
- **No subscription to AutoTrader or eBay Motors Pro required.**

### Common Objections

| Objection | Response |
|---|---|
| "I already do this myself" | "How long does it take you per car? This runs overnight and hands you the shortlist." |
| "How accurate is the valuation?" | "It uses live AutoTrader comparable sales — same data you'd check manually, just automated." |
| "What if the car's already sold?" | "eBay timestamps are on the card — BUY picks are usually flagged within hours of listing." |
| "I don't buy from eBay" | "The same engine covers Facebook Marketplace and Motors.co.uk. You pick the platform." |
| "What does it cost?" | "I'm still in testing — right now I want feedback. If it saves you one bad buy a month, the maths works." |

### Close

> "Happy to run a custom scan for your price bracket and radius tonight —
> free, no strings. If the picks are rubbish, you tell me. If three of them
> end up on your forecourt, we can talk terms."

---

## Pricing Ideas (To Test)

| Tier | Price | Includes |
|---|---|---|
| Starter | £49/month | 1 daily run, email delivery, up to £5k price band |
| Pro | £129/month | Unlimited runs, custom queries, Facebook + eBay + Motors |
| Agency | £299/month | Multi-user, 3 dealer seats, white-label report |

**Alternative:** per-report pricing (£5–£15/run) for low-volume buyers.

---

## Demo Checklist

Before meeting a dealer, make sure your demo run shows:

- [ ] At least 2–3 BUY or OFFER cards with realistic profit (£100–£400 range)
- [ ] At least one car with a VRM + clickable MOT history
- [ ] The listing link works (opens eBay or Facebook)
- [ ] Dark mode looks clean if they view it on their phone
- [ ] Gallery arrows work on any multi-image card

**Tip:** Run the tool the night before with `DEALERLY_FB_MAX_LISTINGS=200` for a faster run.
