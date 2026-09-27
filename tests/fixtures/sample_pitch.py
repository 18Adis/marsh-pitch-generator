"""Realistic sample PitchContent + CompanyProfile (Infosys) for rendering tests — no API calls."""
from src.generation.pitch_generator import PitchContent, BenefitMapping, RecommendedPolicy
from src.generation.company_profile import CompanyProfile, SourcedFact

PROFILE = CompanyProfile(
    industry="Information technology services and consulting",
    size_estimate="3,28,594 employees (31 March 2026)",
    key_risks=[],
    assumptions_used=[
        "Key risks are inferred from the nature of IT services work, not from Infosys health data.",
        "Share of employees on overseas assignments is not disclosed; assumed material given 59-country presence.",
    ],
    facts=[
        SourcedFact(label="Employees", value="3,28,594 employees as of March 31, 2026", source_id="S1",
                    source_title="Infosys Integrated Annual Report 2025-26",
                    source_url="https://www.infosys.com/investors/reports-filings/annual-report/annual/documents/infosys-ar-26.pdf",
                    verified_in_source=True),
        SourcedFact(label="Revenue", value="₹1,78,650 crore total revenues in fiscal 2026", source_id="S1",
                    source_title="Infosys Integrated Annual Report 2025-26",
                    source_url="https://www.infosys.com/investors/reports-filings/annual-report/annual/documents/infosys-ar-26.pdf",
                    verified_in_source=True),
        SourcedFact(label="Active clients", value="1,965 active clients", source_id="S1",
                    source_title="Infosys Integrated Annual Report 2025-26",
                    source_url="https://www.infosys.com/investors/reports-filings/annual-report/annual/documents/infosys-ar-26.pdf",
                    verified_in_source=True),
        SourcedFact(label="Global presence", value="Offices in 59 countries", source_id="S3",
                    source_title="Infosys global presence", source_url="https://www.infosys.com/about/global-presence.html",
                    verified_in_source=True),
        SourcedFact(label="Headquarters", value="Bengaluru, India", source_id="S2", source_title="Infosys - Wikipedia",
                    source_url="https://en.wikipedia.org/wiki/Infosys", verified_in_source=True),
    ],
    data_source="live_web",
    sources=[
        {"id": "S1", "title": "Infosys Integrated Annual Report 2025-26",
         "url": "https://www.infosys.com/investors/reports-filings/annual-report/annual/documents/infosys-ar-26.pdf"},
        {"id": "S2", "title": "Infosys - Wikipedia", "url": "https://en.wikipedia.org/wiki/Infosys"},
        {"id": "S3", "title": "Infosys global presence", "url": "https://www.infosys.com/about/global-presence.html"},
        {"id": "S4", "title": "Infosys ESG Report 2025-26 — Employee wellbeing", "url": "https://www.infosys.com/sustainability/esg-report.pdf"},
    ],
)

PITCH = PitchContent(
    company_name="Infosys",
    industry="Information technology services and consulting",
    size_estimate="3,28,594 employees (31 March 2026)",
    key_risks_summary=[
        "Sedentary desk work: long hours at screens raise the risk of diabetes, high blood pressure, high cholesterol and obesity across a large desk-based workforce.",
        "Overseas client assignments: employees regularly travel and work onsite at client locations abroad, where treatment costs are high.",
        "Rising medical costs and repeat hospitalisation: a large family-floater population needs cover that grows and restores after claims.",
        "Out-of-pocket non-medical expenses: consumables such as gloves and masks can add 10-20% to a hospital bill.",
        "Dependent parents: many employees support ageing parents whose hospitalisation costs are significant.",
    ],
    why_marsh=("Marsh brings an independent, insurer-neutral view: we compared four leading health plans against "
               "Infosys' own workforce risks rather than an insurer's sales script. Every benefit in this proposal is "
               "traced to the insurer's brochure, and anything we could not verify is flagged for review. We support "
               "plan structuring, insurer negotiation and enrolment at the scale of a 3.28 lakh workforce. Our advisors "
               "stay with your employees at claim time, from cashless approvals to escalations."),
    benefits=[
        BenefitMapping(risk="Sedentary desk work: lifestyle conditions", matched_benefit="Activ One: Day 1 cover for 7 listed chronic conditions — asthma, high blood pressure, high cholesterol, diabetes, COPD, obesity and coronary artery disease — with zero waiting period.",
                       source_policy="Activ One", page_number=2, exact_quote_from_source="Now get Day 1 cover for listed 7 chronic conditions with zero waiting period"),
        BenefitMapping(risk="Overseas client assignments", matched_benefit="Activ One VIP+: global cover for emergency and planned treatment abroad, with international and domestic sum insured from ₹50 lakh to ₹6 crore.",
                       source_policy="Activ One", page_number=2, exact_quote_from_source="For emergency and planned treatments abroad"),
        BenefitMapping(risk="Rising medical costs and repeat hospitalisation", matched_benefit="HDFC ERGO Optima Secure+: base cover doubles from Day 1 (Secure Benefit), 100% of base sum insured is added every year irrespective of claims, and cover is restored unlimited times.",
                       source_policy="HDFC ERGO's Optima Secure+", page_number=2, exact_quote_from_source="Your base cover doubles instantly, at no extra cost"),
        BenefitMapping(risk="Out-of-pocket non-medical expenses", matched_benefit="Activ One Claim Protect: covers listed non-medical expenses such as gloves, gown and face masks.",
                       source_policy="Activ One", page_number=1, exact_quote_from_source="takes care of 100% out-of-pocket expenses"),
        BenefitMapping(risk="Dependent parents", matched_benefit="Activ One SAVR: ₹500 per day hospital cash for parents (up to 30 days) and additional tax deduction of up to ₹50,000 by declaring parents.",
                       source_policy="Activ One", page_number=2, exact_quote_from_source="INR 500 per day (maximum up to 30 days)"),
    ],
    recommended_policy=RecommendedPolicy(
        policy_name="Aditya Birla Health Insurance — Activ One",
        justification=("Aditya Birla Health Insurance — Activ One is the only plan of the four that covers Infosys' two most distinctive exposures: "
                       "lifestyle conditions from Day 1 and treatment abroad. Care Supreme and HDFC ERGO only cover chronic conditions after a "
                       "30-day wait, and none of the other three brochures describes global cover. HDFC ERGO Optima Secure+ remains the "
                       "alternative if claims track record is the priority."),
        key_talking_points=[
            "Activ One VIP+ protects employees on overseas assignments with global cover up to ₹6 crore.",
            "Day 1 cover for diabetes, blood pressure and 5 other chronic conditions — no waiting period.",
            "SAVR extends protection to parents, with daily hospital cash and extra tax savings.",
        ],
    ),
    assumptions_used=["Exact premium depends on zone, sum insured and age bands; quotes are required.",
                      "Existing Infosys group mediclaim cover has not been reviewed."],
)
