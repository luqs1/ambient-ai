# Ambient: employee monitoring and privacy law (EU, UK, US)

*Research memo, 10 November 2026. This is background research for the founder and a later lawyer review. It is not legal advice.*

## TL;DR

**Top five findings**

1. **Keeping data away from the employer is the strongest legal fact Ambient has, but it doesn't take the employer out of the law.** In the EU, a company that picks, pays for and rolls out a tool for a business purpose can be a GDPR **controller even if it never sees the data** ([CJEU C‑683/21, paras 35–38](https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=celex%3A62021CJ0683)). In Germany, works-council co-determination depends on whether a tool is *objectively capable* of monitoring, not on whether anyone intends to monitor ([BAG 1 ABR 7/15, as applied in ArbG Hamburg 24 BVGa 1/24](https://recht.nulegal.eu/rechtsprechung/arbg-hamburg/2024-01-16/24-bvga-1-24)). California's new laws define a "workplace surveillance tool" as any app that "collects or facilitates the collection of employee … communications" ([AB 1883, Lab. Code §1580(g)](https://leginfo.legislature.ca.gov/faces/billTextClient.xhtml?bill_id=202520260AB1883)), and Ambient fits that definition word for word.
2. **The cloud agent account is the biggest hidden leak.** Suppose employees run Claude Code or Codex under the company's Enterprise workspace. Admins can then pull the **session transcripts from users' machines** through the Compliance API ([Anthropic](https://platform.claude.com/docs/en/manage-claude/compliance-api), [OpenAI](https://help.openai.com/en/articles/9261474-openai-compliance-platform-for-enterprise-and-edu-customers)), so "company sees: nothing" is false in that setup. Suppose instead they use personal consumer accounts. Colleagues' and customers' messages can then be kept for 5 years and used for training if the user opted in ([Anthropic consumer terms](https://www.anthropic.com/news/updates-to-our-consumer-terms)), and there is no data processing agreement. That is the same fact pattern that kept the wiretap claims alive in *In re Otter.ai* (below).
3. **Decrypting traffic on the device is low risk; forwarding to an LLM is where the interception risk sits.** Because the user is a party to, or intended recipient of, what they read, the local decryption falls outside the US federal Wiretap Act ([18 U.S.C. §2511(2)(d)](https://www.law.cornell.edu/uscode/text/18/2511)), outside UK "interception" ([IPA 2016 s.4(1)(b)](https://www.legislation.gov.uk/ukpga/2016/25/section/4)) and outside the EU ePrivacy ban on interception "by persons other than users" ([Dir. 2002/58 Art. 5(1)](https://eur-lex.europa.eu/legal-content/EN/ALL/?uri=CELEX:32002L0058)). The exposure is California's all-party CIPA §631. Courts there treat an AI vendor that *can* or *does* use conversations for its own purposes as a third-party eavesdropper ([Ambriz v. Google](https://fbtgibbons.com/cipa-litigation-and-the-technological-capability-to-violate-californias-privacy-laws/); [In re Otter.ai, N.D. Cal. 13 Aug 2026](https://cases.justia.com/federal/district-courts/california/candce/5:2025cv06911/454675/68/0.pdf)).
4. **The workplace AI rules don't bite on the current design, but three triggers need guardrails.** Ambient is not high-risk under the EU AI Act's employment category (Annex III(4)) or a "significant decision" under the CPPA's automated decision-making rules, as long as triggers stay personal and nothing evaluates people. Those high-risk AI Act duties now start **2 Dec 2027** ([Reg. 2026/1744](https://eur-lex.europa.eu/eli/reg/2026/1744/oj/eng)). The three guardrails:
   - **Emotion inference about employees.** California AB 1883 bans it from **1 Jan 2027**, and unlike the AI Act it covers inference from *text*.
   - **Triggers that allocate work to others.** "Allocation of work" counts as a significant decision under [CPPA ADMT](https://www.cppa.ca.gov/regulations/ccpa_updates.html).
   - **Marketing that hints at evaluating people.** The AI Act classifies a system by its intended purpose, and the provider's marketing defines that purpose.
5. **Several page claims are false in common setups.** "Nothing is collected centrally", "personal info never reach the model", "company sees: nothing" and "Local and offline by design" are untrue or unsubstantiated in at least one ordinary configuration. The FTC applies §5 to claims made to *business* buyers too: in May 2026 it settled with Cox Media Group over AI and voice-data claims made to small-business customers ([Ars](https://arstechnica.com/tech-policy/2026/05/marketer-that-claimed-it-could-tap-devices-for-ad-targeting-will-pay-880k-settlement/)).

**Recommended teams model: "employee-held".**
- **What the company does:** it buys licences and stops there. Each employee installs and opts in. Device management (MDM) may *offer* the app but can't enable it or push triggers.
- **What the company can't see:** there's no admin console, no aggregate stats and no telemetry. The local store is encrypted with a key in the user's keychain and kept only briefly.
- **Which model runs:** a local model is the default. A cloud agent runs only on a company-approved **commercial API key with no training and zero data retention (ZDR)**, never on an Enterprise workspace linked to the Compliance API, and never on a consumer account.
- **Whose content is read:** work apps only. Personal apps (Discord, personal browsing) are off by default.
- **What triggers may do:** they're about the user's own work. Triggers that score, profile, infer emotions about or assign work to others are blocked.
- **What agents send:** outbound messages are drafts by default; a person sends them.
- **What the company gets:** a paperwork pack with a DPIA template, an employee notice and a works-council/CSE information sheet.

---

## EU (GDPR, ePrivacy, AI Act)

**1. Is it "monitoring"? Who is the controller?**
- **The employee's Mac is processing other people's data.** GDPR applies there: "processing" means any operation on personal data ([Art. 4(2)](https://eur-lex.europa.eu/eli/reg/2016/679/oj)).
- **Company-deployed:** the company decides the purpose ("less admin") and the essential means (which tool, which apps, which LLM). That makes it a controller even though it never receives the data, because controllership doesn't require access ([C‑683/21](https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=celex%3A62021CJ0683)). Employees act under its authority (Art. 29).
- **Employee-chosen on their own device for their own purposes:** the employee may be the controller. For personal Discord chats with friends, the household exemption may apply ([Art. 2(2)(c)](https://eur-lex.europa.eu/eli/reg/2016/679/oj)), though courts read it narrowly. The employer is out of the picture unless it sanctioned or directed the use.
- **Ambient is neither controller nor processor** while it receives nothing. Any crash report, licence ping or update check that carries personal data changes that.
- **Not "monitoring" in the WP29 sense, but still regulated:** with no employer access and no evaluation, this isn't employee monitoring as [WP29 Opinion 2/2017 (WP249)](https://ec.europa.eu/newsroom/article29/items/610169) describes it. It is still systematic processing of communications content, so it needs:
  - **A lawful basis:** realistically legitimate interests (Art. 6(1)(f)). Employee consent is "highly unlikely" to be valid (WP249).
  - **Data minimisation and privacy by design** ([Arts 5, 25](https://eur-lex.europa.eu/eli/reg/2016/679/oj)).
  - **Probably a DPIA ([Art. 35](https://eur-lex.europa.eu/eli/reg/2016/679/oj)):** the case meets at least two of the [WP248](https://ec.europa.eu/newsroom/article29/items/611236) criteria (employees as vulnerable data subjects, innovative technology, systematic processing of communications).
- **National employee-data rules (Art. 88):** after [C‑34/21](https://www.baden-wuerttemberg.datenschutz.de/faq-rechtsgrundlagen-bei-beschaeftigtendaten/), a national general clause can't serve as a standalone legal basis. Works agreements remain a valid route under Art. 88.
- **ePrivacy terminal-equipment rule:** installing a root certificate and a network extension is "storing information in terminal equipment" under ePrivacy Art. 5(3), which needs the subscriber's or user's consent. On BYOD that means the employee's consent, which an opt-in flow covers.

**2. Third parties (colleagues, customers, friends)**
- **Interception:** none here. Art. 5(1) is aimed at persons *other than* users, and the Ambient user is a party to the communication.
- **Data protection:**
  - **Privacy notice:** the company's notice should cover AI processing of incoming messages (Art. 14; Art. 14(5)(b) may relieve direct notice to each person).
  - **Special-category data:** health and similar data turning up in messages has no legitimate-interests basis (Art. 9), so Shield has to cover the triggering message itself, not only tool outputs.
  - **Processor terms:** an LLM on a company commercial account is a processor under a data processing agreement (Art. 28).
  - **Consumer accounts:** with a consumer account the provider becomes an independent controller, possibly training on third-party data. That is hard to justify.
- **US transfers:** the EU–US Data Privacy Framework is still valid. The General Court upheld it in *Latombe* (T‑553/23, 3 Sep 2025), and the appeal [C‑703/25 P](https://eur-lex.europa.eu/eli/C/2025/6610/oj/eng) is pending with no hearing yet. Keep standard contractual clauses as a fallback.

**3. EU AI Act**
- **Roles:** Ambient is the *provider* of an AI system (classifier plus orchestration). The company is the *deployer*.
- **Not high-risk:** Annex III(4)(b) covers systems *intended* to decide on promotion or termination, allocate tasks based on behaviour or traits, or monitor and evaluate performance. Personal, user-written triggers fall outside it. Because classification follows the provider's stated purpose, the Commission's 2026 draft guidance tells multi-purpose systems to exclude high-risk uses consistently across marketing and terms ([Paul Weiss summary](https://www.paulweiss.com/insights/client-memos/next-stop-on-the-digital-omnibus-ai-act-reforms-and-new-commission-guidance)).
- **Emotion recognition ban:** Art. 5(1)(f) covers only inference from *biometric* data. Sentiment analysis of text is out of scope ([Commission guidelines, paras 245–251](https://ai-act-service-desk.ec.europa.eu/sites/default/files/2026-01/guide-prohibited_en.pdf)).
- **Transparency:** the Art. 50 duties apply from 2 Aug 2026. If an agent *auto-sends* replies to customers, those people are interacting with an AI system, so keep a human pressing send.
- **AI literacy:** Art. 4 remains a softened, means-based duty for deployers.

**Germany**
- **§26 BDSG:** §26(1)1 doesn't meet Art. 88 ([BAG 1 ABR 14/22 via commentary](https://caroline-herrmann.com/beschaeftigtendatenschutz/)), so employers rely on Art. 6 GDPR directly. A new Beschäftigtendatengesetz has been announced, but no draft had appeared as of mid-2026 ([riehnlegal](https://riehnlegal.blog/content/artikel/?item=beschaeftigtendatenschutz-2026-der-zweite-anlauf-zum-eigenen-gesetz-und-was-er-fuer-dich-bedeutet)).
- **Works council, [§87(1) Nr. 6 BetrVG](https://www.gesetze-im-internet.de/betrvg/__87.html):** a technical device is "intended" to monitor if it is objectively capable of letting the *employer* record behaviour or performance data. Intent doesn't matter.
  - Ambient on company Macs with a readable local log of who wrote what, which IT admins can open, very likely triggers co-determination.
  - **Escape route:** ArbG Hamburg (2024) found no co-determination for ChatGPT used through employees' own accounts, because the "monitoring pressure" came from the vendor and the employer couldn't access the data.
  - Ambient should design toward that ruling: employee-held accounts, a store the employer can't read, nothing installed by the employer.
  - Where a works council exists (only if one has been elected; businesses with 5+ employees can elect one), a short works agreement (Betriebsvereinbarung) is still the cheap, safe path, and it doubles as an Art. 88 basis.

**France ([CNIL, updated July 2026](https://cnil.fr/fr/controle-de-lactivite-des-personnes-employees))**
- **Rules for any device that controls activity:**
  - it must be justified and proportionate;
  - the works council (CSE) must be consulted beforehand where the company has 50+ employees (C. trav. L2312‑38);
  - employees must be informed beforehand (L1222‑4), at any company size.
- **What CNIL sanctions:** it treats keyloggers, regular screenshots and tools that capture private messages as disproportionate "permanent surveillance". It fined one company €40k for this ([decision summary](https://cnil.champeau.info/decisions/505)).
- **How Ambient will look:** a network-level TLS interceptor on company laptops resembles those tools. Brief the CSE that it is *not* a control device, back that with technical proof, and exclude personal apps.

## UK (UK GDPR, DPA 2018, DUAA 2025, IPA 2016)

**1. Is it monitoring?**
- **Controller analysis:** the same as in the EU.
- **ICO guidance:** the [monitoring-workers guidance](https://cy.ico.org.uk/for-organisations/uk-gdpr-guidance-and-resources/employment/monitoring-workers/data-protection-and-monitoring-workers/) says the employer is "responsible for deciding how and why" any third-party tool is used. A DPIA should consider customers and others captured. Employers should separate content from metadata and access content only exceptionally.
- **Data (Use and Access) Act 2025:** all of its data-protection provisions are now in force ([ICO](https://ico.org.uk/about-the-ico/what-we-do/legislation-we-cover/data-use-and-access-act-2025/the-data-use-and-access-act-2025-what-does-it-mean-for-organisations)):
  - most commenced 5 Feb 2026;
  - the duty to run a complaints procedure commenced 19 Jun 2026, and every controller company needs one;
  - the ICO became the Information Commission on 30 Sep 2026 ([SI 2026/1015](https://www.legislation.gov.uk/uksi/2026/1015/made/data.html)).
- **Which DUAA changes matter:** the new automated-decision-making rules (s.80) only reach significant decisions, so they don't apply here. The new "recognised legitimate interests" don't cover productivity. The relaxed PECR terminal-equipment rules (s.112) matter only at the margin.

**2. Interception and third parties**
- **The offence:** under [IPA s.3](https://www.legislation.gov.uk/ukpga/2016/25/section/3), it is an offence to intentionally intercept, in the course of transmission, without lawful authority.
- **Local decryption:** this is not interception, because s.4(1)(b) requires content to be made available to someone *other than* the sender or intended recipient.
- **Forwarding to the LLM provider:** the provider *is* such a third party. If Ambient forwards from the endpoint after delivery, that is best read as the recipient's own disclosure, which falls under data protection. But s.4(4)–(5) ("stored in or by the system", "diverted or recorded … so as to make available … after that time") leave room for argument.
- **Consent and exemptions:** consent-based authority needs *both* parties to consent ([s.44(1)](https://www.legislation.gov.uk/ukpga/2016/25/section/44)). The exemption for whoever controls a system (s.3(2)) covers only private systems, and Slack over the internet isn't one. The Lawful Business Practice Regulations ([SI 2018/356](https://www.legislation.gov.uk/uksi/2018/356/contents/made)) only matter if the company itself monitors.
- **Design rule:** forward from the local store after the message is delivered, never inline in the network stream.

**3. AI:** the UK has no AI statute, and the DUAA's automated-decision-making rules don't apply.

## US

**1. Is it monitoring?**
- **Federal:** there's no general federal monitoring statute.
- **State notice laws:** these cover an employer that "monitors or otherwise intercepts" employee email or transmissions:
  - **New York:** [Civ. Rights Law §52‑c](https://www.nysenate.gov/legislation/laws/CVR/52-C) requires notice at hire, an acknowledgement and a posted notice.
  - **Delaware:** [19 Del. C. §705](https://delcode.delaware.gov/title19/c007/index.html).
  - **Connecticut:** §31‑48d was repealed and replaced by PA 26‑73, effective 1 Oct 2026 ([NatLawReview](https://natlawreview.com/article/deadline-imminent-connecticuts-expanded-electronic-monitoring-law)).
  - **Maine:** [26 M.R.S. §620‑A](https://www.preti.com/publications/maine-employers-are-subject-to-heightened-compliance-scrutiny-and-new-workplace-surveillance-rules-starting-july-14/), effective 14 Jul 2026. It requires notice and **bans forcing employees to install data-collection apps on personal devices**.
- **Do they reach Ambient?** Whether an employer-funded tool the employer never reads counts as "monitoring" is untested. Giving notice is cheap.
- **California:**
  - [AB 1331](https://leginfo.legislature.ca.gov/faces/billNavClient.xhtml?bill_id=202520260AB1331) (bathrooms) and AB 1883 define "workplace surveillance tool" broadly enough to cover Ambient when the *employer uses* it.
  - AB 1883 bans AI tools that infer an employee's emotional state, from 1 Jan 2027, with no biometric limitation.
  - The broad notice bill AB 1221 died in Feb 2026.
- **CCPA:** employees have been "consumers" since 2023, but only for companies that meet the [§1798.140(d)](https://leginfo.legislature.ca.gov/faces/codes_displaySection.xhtml?lawCode=CIV&sectionNum=1798.140) thresholds, which most sub-100-person companies won't.
- **NLRB:** the General Counsel's surveillance memo GC 23‑02 was rescinded in Feb 2025 ([GC 25‑05](https://www.nlrb.gov/news-outreach/news-story/gc-25-05-rescission-of-certain-general-counsel-memoranda)). Board law on surveillance, and on creating an "impression of surveillance", hasn't changed. A design with no employer access is the answer here.

**2. Wiretap and third parties**
- **Federal Wiretap Act:** a party may intercept a communication unless it does so for a criminal or tortious purpose ([§2511(2)(d)](https://www.law.cornell.edu/uscode/text/18/2511)). The user is a party, including in channels they belong to. The business-use exception ([§2510(5)(a)](https://www.law.cornell.edu/uscode/text/18/2510)) only matters for employer monitoring.
- **Stored Communications Act:** [§2701](https://www.law.cornell.edu/uscode/text/18/2701) isn't engaged, because the user is authorised on their own accounts.
- **All-party-consent states:** CA, FL, IL, MD, MA, MT, NH, PA, WA and others ([Justia survey](https://www.justia.com/50-state-surveys/recording-phone-calls-and-conversations/)).
- **The live risk is [CIPA §631](https://leginfo.legislature.ca.gov/faces/codes_displaySection.xhtml?lawCode=PEN&sectionNum=631).** It applies when a *third party* reads content "in transit" without every party's consent.
  - **Ambient itself:** with no server, Ambient is an "extension" of the user (*Graham v. Noom*).
  - **The LLM provider:** it's the candidate eavesdropper. *Ambriz v. Google* used a "capability" test, and Google's motion was re-argued in Oct 2026 ([CNS](https://www.courthousenews.com/judge-skeptical-of-claims-google-ai-tool-wiretapped-customer-service-calls/)). *Otter.ai* held that a vendor which retains conversations and trains on them is a third party, and the federal wiretap (ECPA) claim survived as well.
  - **Meeting transcripts:** these add CIPA §632 exposure for spoken conversations.
- **Mitigations:**
  - commercial no-training ZDR keys, or a local model;
  - forwarding after delivery;
  - meeting banners that mention AI processing.

**3. AI rules**
- **[CPPA ADMT rules](https://cppa.ca.gov/announcements/2025/20250923.html):** in effect since 1 Jan 2026, with ADMT compliance due 1 Jan 2027. They cover technology that replaces human decisions on hiring, allocation of work, pay, promotion or termination. Personal triggers don't do that; a trigger that *assigns work to colleagues* could.
- **[SB 947 "No Robo Bosses"](https://ogletree.com/insights-resources/blog-posts/california-governor-signs-3-bills-targeting-ai-and-workplace-surveillance/):** from 1 Jul 2027 it covers discipline and termination decisions only, so it doesn't apply.

**FTC Act §5**
- **The test:** a representation that is likely to mislead and is material is deceptive ([1983 Deception Policy Statement](https://www.ftc.gov/legal-library/browse/1983-ftc-policy-statement-deception)).
- **Marketing promises count:** privacy promises in marketing bind the company, wherever they are made ([FTC, Jan 2024](https://www.ftc.gov/policy/advocacy-research/tech-at-ftc/2024/01/ai-companies-uphold-your-privacy-confidentiality-commitments)).
- **B2B buyers are covered:** the Cox Media Group settlement protected business customers.

## Recommended design and deployment (what keeps it outside "employee monitoring")

| Guarantee | Why |
|---|---|
| Employees opt in, with no consequence for declining. MDM may distribute the app but can't enable it or set triggers. No install on personal devices unless the employee asks. | Takes the employer out of "purposes and means"; fits the Hamburg ruling; complies with Maine §620‑A; makes "hold the switch" true |
| No admin console, aggregate stats, telemetry or usage-revealing licence check | Supports the C‑683/21 analysis, CIPA "extension" status and the marketing claims |
| Local SQLite encrypted with a key in the user's keychain; short retention (say 7 days for raw text); excluded from backups | Removes the employer's *capability* to read it (§87 BetrVG, CNIL); limits e‑discovery exposure |
| Local model as the default agent. Cloud agents only on a company-approved **commercial API key with no training and ZDR**. Consumer accounts and Compliance-API-linked Enterprise workspaces blocked or prominently disclosed | Data processing agreement in place; no training (Otter); no admin access to transcripts |
| Shield applied to the **triggering message** as well as tool outputs, including special-category data | Art. 9; substantiates the redaction claims |
| Work apps only in team mode. Discord and personal browsing off by default and blocked on company-managed installs | Limits processing of third parties' and private data; CNIL proportionality |
| Triggers limited to the user's own work. Block emotion inference about people, scoring or evaluating colleagues, and assigning work to others | CA AB 1883, CPPA ADMT, AI Act Annex III(4) |
| Outbound messages are drafts by default; a person presses send | AI Act Art. 50; accuracy |
| Agent sandboxed so it can't read the Ambient database or arbitrary files | Makes "one message, never your whole day" true |
| Paperwork pack: DPIA template, legitimate-interests assessment, employee notice (with NY/CT/DE/ME wording), customer privacy-notice paragraph, works-council/CSE information sheet and model works agreement, terms of use excluding monitoring and evaluation | Lets a small company show it complies in a day |

## Marketing claims review

**Applicable law:**
- **US:** FTC §5.
- **UK:** for B2B, the [Business Protection from Misleading Marketing Regulations 2008](https://www.legislation.gov.uk/uksi/2008/1276/contents) and the [CAP Code](https://www.asa.org.uk/codes-and-rulings/advertising-codes/non-broadcast-code.html) (rule 3.7: substantiation held *before* publishing). For the consumer-facing page, the DMCC Act 2024, which the CMA enforces with direct fines.
- **EU:** for B2B, the [Misleading Advertising Directive 2006/114](https://eur-lex.europa.eu/eli/dir/2006/114/oj); for consumers, the [Unfair Commercial Practices Directive 2005/29](https://eur-lex.europa.eu/eli/dir/2005/29/oj) (Art. 7: misleading omissions). In Germany, competitors can enforce through §5 UWG cease-and-desist letters.

**Omission to fix on both pages:** neither page says that Ambient installs a root certificate and decrypts TLS, or that it processes other people's messages. Both facts are material to buyers and to their DPIA, so disclose them plainly on a "How it works / Security" page.

| Claim (page) | Verdict | Why / safer wording |
|---|---|---|
| "Agents for every employee. Nothing to monitor." (teams hero and meta) | **Risky** | Absolute. Ambient reads communications, and the company *can* see transcripts via an Enterprise LLM account or the local disk. → "Agents for every employee. No monitoring dashboard." |
| "Each person writes their own triggers, checked on their own Mac." | **Fine** | True if admins can't push triggers. |
| "Your company gets the time back, not a record of what anyone did." | **Needs guarantee** | Needs the encrypted store and the block on Compliance-API accounts. |
| "What the company sees: No activity log. No dashboard of people." | **Fine** (qualify) | → "Ambient gives the company no activity log and no dashboard of people." |
| "Local code and an offline model decide. Nothing is collected centrally." | **Risky** | The AI provider *does* collect the matched messages centrally. → "Ambient has no server. Only messages that match a trigger go to the agent each person picks." |
| "Claude, Codex, or a local model. Their choice." | **Fine** (align) | Conflicts with requiring approved accounts. → "…on accounts your company approves." |
| "Built so there's nothing to surveil." / "company sees: nothing" | **Risky** | Third parties' messages are read and the LLM provider sees matches. → "Built so the company has nothing to look at." / "company sees: no activity log" |
| "No central log … There's no company view of it." / meta "no central record of anyone's activity" | **Needs guarantee** | Requires no telemetry and the Enterprise-account caveat. |
| "Employees hold the switch … can pause any time." | **Needs guarantee** | MDM must not override it, and pausing must stop decryption. |
| "Keys and personal info are redacted before any AI provider sees them." / "Keys, tokens and personal info never reach the model." | **Risky** | Detection is probabilistic, and Shield must also cover the triggering message. → "Shield detects and redacts common secrets and personal data before prompts reach an AI provider." |
| "Checked on-device, works offline" / "Every trigger is checked on your Mac" | **Fine** | Trigger checks only; cloud agents need a network. |
| "nothing sent by default" | **Needs guarantee** | A fresh install must send nothing, including update checks and crash reports. |
| "Local and offline by design." / "Local. Offline. Yours." | **Risky** | Cloud agents are a common path. → "Local-first: triggers are checked on your Mac." |
| "Proactive agents, without the surveillance." (main) | **Risky** (moderate) | Implied absolute claim. → "Proactive agents that don't watch your whole day." |
| "Agents see one message, never your whole day." / "Only matches leave: One message … Nothing else." | **Needs guarantee** | The agent CLI can read files, including the Ambient database, and can call tools. Sandbox it, or → "Ambient sends the agent only the matched message." |
| "Restored on your Mac: Your agent still gets the real value." | **Fine** | — |
| "Plain code reads your apps." | **Fine** | But disclose the TLS decryption (see the omission note above). |

## Open questions for a lawyer

1. **EU controller role:** with "employee-held" deployment, is the company still a controller or joint controller with each employee? Can the paperwork position the employee as the controller for their own triggers? Does the household exemption cover personal Discord on a work-licensed app?
2. **Germany:** does the ArbG Hamburg reasoning extend to an *installed* endpoint tool with an encrypted store on company devices? Is a works agreement still advisable where a works council exists?
3. **UK:** is forwarding a delivered message from the endpoint "interception" under IPA s.4(4)–(5)?
4. **US state notice laws:** do the NY, DE, CT and ME duties, and California AB 1331/1883 ("employer uses"), attach to a tool the employer pays for but can't access?
5. **CIPA §631:** is an LLM provider under no-training ZDR terms an "extension" of the user? Is near-real-time forwarding "in transit"? Watch the next *Ambriz* ruling.
6. **ePrivacy on company devices:** how do Art. 5(3) and national rules (e.g. Germany's TDDDG §25, and §3 where private use is allowed) apply to installing a root certificate on employer devices?
7. **Missed special-category data:** what is the fallback position if Shield misses Art. 9 data in a forwarded message?
8. **Ambient's own exposure:** its status as AI Act provider (Art. 50, intended-purpose wording), acceptable-use terms (no monitoring of others, no emotion triggers), and whether to offer indemnities.
9. **Litigation holds:** do they reach local, encrypted, short-retention data on company devices? Does a short retention default create spoliation risk?
10. **Claims file:** sign off on the substantiation for each claim kept (CAP 3.7, FTC).

## Sources

**EU:**
- [GDPR](https://eur-lex.europa.eu/eli/reg/2016/679/oj)
- [ePrivacy Dir. 2002/58](https://eur-lex.europa.eu/legal-content/EN/ALL/?uri=CELEX:32002L0058)
- [CJEU C‑683/21](https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=celex%3A62021CJ0683)
- [C‑34/21 explainer (LfDI BW)](https://www.baden-wuerttemberg.datenschutz.de/faq-rechtsgrundlagen-bei-beschaeftigtendaten/)
- [WP249](https://ec.europa.eu/newsroom/article29/items/610169)
- [WP248 DPIA](https://ec.europa.eu/newsroom/article29/items/611236)
- [AI Omnibus Reg. 2026/1744](https://eur-lex.europa.eu/eli/reg/2026/1744/oj/eng)
- [Commission: AI Omnibus in force](https://digital-strategy.ec.europa.eu/en/news/ai-omnibus-enters-force)
- [Commission guidelines on prohibited practices](https://ai-act-service-desk.ec.europa.eu/sites/default/files/2026-01/guide-prohibited_en.pdf)
- [Latombe appeal C‑703/25 P](https://eur-lex.europa.eu/eli/C/2025/6610/oj/eng)
- [Dir. 2006/114](https://eur-lex.europa.eu/eli/dir/2006/114/oj)
- [Dir. 2005/29](https://eur-lex.europa.eu/eli/dir/2005/29/oj)

**Germany and France:**
- [§87 BetrVG](https://www.gesetze-im-internet.de/betrvg/__87.html)
- [ArbG Hamburg 24 BVGa 1/24](https://recht.nulegal.eu/rechtsprechung/arbg-hamburg/2024-01-16/24-bvga-1-24)
- [BeschDG status (riehnlegal, Jun 2026)](https://riehnlegal.blog/content/artikel/?item=beschaeftigtendatenschutz-2026-der-zweite-anlauf-zum-eigenen-gesetz-und-was-er-fuer-dich-bedeutet)
- [CNIL: contrôle de l'activité](https://cnil.fr/fr/controle-de-lactivite-des-personnes-employees)
- [CNIL telework Q&A](https://www.cnil.fr/fr/les-questions-reponses-de-la-cnil-sur-le-teletravail)

**UK:**
- [IPA s.3](https://www.legislation.gov.uk/ukpga/2016/25/section/3)
- [IPA s.4](https://www.legislation.gov.uk/ukpga/2016/25/section/4)
- [IPA s.44](https://www.legislation.gov.uk/ukpga/2016/25/section/44)
- [SI 2018/356](https://www.legislation.gov.uk/uksi/2018/356/contents/made)
- [ICO monitoring workers](https://cy.ico.org.uk/for-organisations/uk-gdpr-guidance-and-resources/employment/monitoring-workers/data-protection-and-monitoring-workers/)
- [ICO on DUAA](https://ico.org.uk/about-the-ico/what-we-do/legislation-we-cover/data-use-and-access-act-2025/the-data-use-and-access-act-2025-what-does-it-mean-for-organisations)
- [DUAA commencement SI 2026/82](https://www.legislation.gov.uk/uksi/2026/82/made/data.html)
- [SI 2026/1015](https://www.legislation.gov.uk/uksi/2026/1015/made/data.html)
- [BPRs 2008](https://www.legislation.gov.uk/uksi/2008/1276/contents)
- [CAP Code](https://www.asa.org.uk/codes-and-rulings/advertising-codes/non-broadcast-code.html)

**US:**
- [18 U.S.C. §2510](https://www.law.cornell.edu/uscode/text/18/2510)
- [18 U.S.C. §2511](https://www.law.cornell.edu/uscode/text/18/2511)
- [18 U.S.C. §2701](https://www.law.cornell.edu/uscode/text/18/2701)
- [Cal. Penal §631](https://leginfo.legislature.ca.gov/faces/codes_displaySection.xhtml?lawCode=PEN&sectionNum=631)
- [AB 1883](https://leginfo.legislature.ca.gov/faces/billTextClient.xhtml?bill_id=202520260AB1883)
- [AB 1331](https://leginfo.legislature.ca.gov/faces/billNavClient.xhtml?bill_id=202520260AB1331)
- [AB 1221 history](https://leginfo.legislature.ca.gov/faces/billHistoryClient.xhtml?bill_id=202520260AB1221)
- [In re Otter.ai order](https://cases.justia.com/federal/district-courts/california/candce/5:2025cv06911/454675/68/0.pdf)
- [Ambriz v. Google (FBT summary)](https://fbtgibbons.com/cipa-litigation-and-the-technological-capability-to-violate-californias-privacy-laws/)
- [Ambriz v. Google (CNS, Oct 2026)](https://www.courthousenews.com/judge-skeptical-of-claims-google-ai-tool-wiretapped-customer-service-calls/)
- [CPPA regulations](https://www.cppa.ca.gov/regulations/ccpa_updates.html)
- [CPPA announcement](https://cppa.ca.gov/announcements/2025/20250923.html)
- [NY CRL §52‑c](https://www.nysenate.gov/legislation/laws/CVR/52-C)
- [19 Del. C. §705](https://delcode.delaware.gov/title19/c007/index.html)
- [CT PA 26‑73](https://natlawreview.com/article/deadline-imminent-connecticuts-expanded-electronic-monitoring-law)
- [Maine §620‑A](https://www.preti.com/publications/maine-employers-are-subject-to-heightened-compliance-scrutiny-and-new-workplace-surveillance-rules-starting-july-14/)
- [NLRB GC 25‑05](https://www.nlrb.gov/news-outreach/news-story/gc-25-05-rescission-of-certain-general-counsel-memoranda)
- [FTC Deception Statement](https://www.ftc.gov/legal-library/browse/1983-ftc-policy-statement-deception)
- [FTC AI privacy commitments](https://www.ftc.gov/policy/advocacy-research/tech-at-ftc/2024/01/ai-companies-uphold-your-privacy-confidentiality-commitments)
- [FTC v. CMG (Ars)](https://arstechnica.com/tech-policy/2026/05/marketer-that-claimed-it-could-tap-devices-for-ad-targeting-will-pay-880k-settlement/)
- [Justia 50-state recording survey](https://www.justia.com/50-state-surveys/recording-phone-calls-and-conversations/)

**LLM providers:**
- [Anthropic consumer terms](https://www.anthropic.com/news/updates-to-our-consumer-terms)
- [Claude Code data usage](https://code.claude.com/docs/en/data-usage)
- [Anthropic Compliance API](https://platform.claude.com/docs/en/manage-claude/compliance-api)
- [OpenAI Compliance Platform](https://help.openai.com/en/articles/9261474-openai-compliance-platform-for-enterprise-and-edu-customers)
