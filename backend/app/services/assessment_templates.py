"""The questionnaires this product ships with.

Content, not machinery. Kept apart from `assessment_service` because these are
a deployment fact — the same reasoning `connectors/registry.py` follows — and
because they will be revised on a completely different rhythm from the code
that runs them.

WHY THESE QUESTIONS AND NOT OTHERS

The DPIA follows the shape §10 implies and the ICO's published structure has
made conventional: establish whether an assessment is needed, describe the
processing, identify the lawful basis, assess necessity and proportionality,
identify risks, and record what will be done about them. The last one is the
part products usually omit and the only part that changes anything.

The RoPA is built from what a regulator actually asks for: purposes,
categories, recipients, transfers, retention, and safeguards.

Every question carries `helper_text`, and that is not decoration. "Explain
broadly what the project aims to achieve" answered without guidance produces a
sentence; answered with a paragraph of prompts about collection, sharing,
retention and third parties it produces a description somebody can assess. A
template with no help is a template that generates confident nonsense.

INDIAN LAW, NOT GDPR WITH THE NAMES CHANGED
`legitimate interests` is not a lawful basis under the DPDP Act, so the DPIA
does not offer it. The bases here are consent and the §7 legitimate uses. A
template that offers a basis the statute does not recognise invites a company to
record a defence it does not have.
"""

from __future__ import annotations

from typing import Any

#: A question, before it becomes a row.
#:
#: `key` is stable across versions so answers can be compared between runs of
#: the same template — "what changed since last year's DPIA" is the question a
#: review cadence exists to make answerable.
Q = dict[str, Any]


def _q(
    key: str,
    prompt: str,
    *,
    type: str = "long_text",
    section: str | None = None,
    helper: str | None = None,
    required: bool = False,
    options: list[str] | None = None,
    reportable: bool = False,
    show_if: dict[str, Any] | None = None,
) -> Q:
    return {
        "key": key,
        "prompt": prompt,
        "type": type,
        "section": section,
        "helper_text": helper,
        "required": required,
        "options": options or [],
        "reportable": reportable,
        "show_if": show_if,
    }


# --------------------------------------------------------------------------- #
# DPIA — §10
# --------------------------------------------------------------------------- #

DPIA: list[Q] = [
    # ---- is one needed at all ------------------------------------------- #
    _q(
        "screening_high_risk",
        "Does this processing involve any of the following?",
        type="multi_choice",
        section="Do you need a DPIA?",
        helper=(
            "Tick everything that applies. Any one of these is a reason to "
            "complete this assessment rather than a reason to be alarmed — the "
            "point of a DPIA is to do the thinking before the processing, not "
            "to prove the processing is dangerous."
        ),
        options=[
            "Personal data of children or persons with a guardian",
            "Systematic monitoring, tracking or behavioural profiling",
            "Automated decisions that affect someone's rights or access to a service",
            "Large volumes of personal data",
            "Financial, health, biometric or government-identity data",
            "Combining datasets that were collected separately",
            "A new technology, or an existing one used in a new way",
            "Transfers of personal data outside India",
            "None of these",
        ],
        required=True,
        reportable=True,
    ),
    _q(
        "screening_conclusion",
        "Is a DPIA required for this processing, and why?",
        section="Do you need a DPIA?",
        helper=(
            "§10 makes a DPIA a duty of a Significant Data Fiduciary. If you "
            "have concluded one is not required, record that conclusion and the "
            "reasoning here — a decision not to assess is itself a decision "
            "somebody may have to defend."
        ),
        required=True,
        reportable=True,
    ),

    # ---- what is actually happening ------------------------------------- #
    _q(
        "description",
        "Explain broadly what this project aims to achieve, and what processing it involves.",
        section="Describe the processing",
        helper=(
            "How will you collect, use, store and delete the data? Where does it "
            "come from? Will you share it with anyone — and if so, who? It is "
            "often easier to describe this as a flow than as prose. Cover the "
            "whole life of the data, including the end of it."
        ),
        required=True,
        reportable=True,
    ),
    _q(
        "data_categories",
        "Which categories of personal data are involved?",
        type="multi_choice",
        section="Describe the processing",
        helper=(
            "Be specific rather than generous. A category listed 'just in case' "
            "widens every obligation that follows from this assessment — "
            "retention, breach notification, and what you have to disclose on a "
            "rights request."
        ),
        options=[
            "Name and contact details",
            "Government identifiers (Aadhaar, PAN, passport, voter ID)",
            "Financial data (accounts, cards, transactions)",
            "Health data",
            "Biometric data",
            "Location data",
            "Behavioural or usage data",
            "Employment or education records",
            "Data about children",
            "Other",
        ],
        required=True,
        reportable=True,
    ),
    _q(
        "volume",
        "Roughly how many people does this affect?",
        type="number",
        section="Describe the processing",
        helper=(
            "An order of magnitude is enough. It matters because scale changes "
            "what a failure costs, not because the number needs to be precise."
        ),
    ),
    _q(
        "retention_period",
        "How long will the data be kept, and what happens at the end?",
        section="Describe the processing",
        helper=(
            "§8(7) requires erasure once the purpose is served and no legal "
            "obligation requires retention. 'As long as necessary' is not an "
            "answer — say what triggers deletion and what actually performs it."
        ),
        required=True,
        reportable=True,
    ),

    # ---- lawful basis --------------------------------------------------- #
    _q(
        "lawful_basis",
        "On what basis is this processing lawful?",
        type="single_choice",
        section="Lawful basis",
        helper=(
            "Under the DPDP Act there are two routes: the person's consent, or "
            "one of the legitimate uses in §7. Note that 'legitimate interests' "
            "is a GDPR concept and is NOT a basis under Indian law — if that is "
            "the reasoning, it needs to fit one of the §7 uses or rest on "
            "consent."
        ),
        options=[
            "Consent (§6)",
            "Voluntarily provided for the purpose, and not withdrawn (§7(a))",
            "Provision of a State subsidy, benefit, service or licence (§7(b))",
            "A function of the State under law (§7(c)-(d))",
            "Compliance with a legal obligation or a court order (§7(e)-(f))",
            "Medical emergency or threat to life (§7(g)-(h))",
            "Disaster or breakdown of public order (§7(i))",
            "Employment purposes (§7(j))",
        ],
        required=True,
        reportable=True,
    ),
    _q(
        "basis_reasoning",
        "Why does that basis apply here?",
        section="Lawful basis",
        helper=(
            "If it is consent: how is it obtained, how is it recorded, and how "
            "does somebody withdraw it as easily as they gave it? If it is a §7 "
            "legitimate use: which limb, and what makes this processing fall "
            "inside it?"
        ),
        required=True,
        reportable=True,
    ),
    _q(
        "notice_given",
        "Is the notice under §5 given before or at the time of collection?",
        type="boolean",
        section="Lawful basis",
        helper=(
            "The notice has to say what data, for what purpose, how to exercise "
            "rights, and how to complain to the Board — and it must be available "
            "in English or any language in the Eighth Schedule."
        ),
        required=True,
        reportable=True,
    ),

    # ---- necessity ------------------------------------------------------ #
    _q(
        "necessity",
        "Is every item of data you are collecting necessary for the stated purpose?",
        section="Necessity and proportionality",
        helper=(
            "Go field by field. The most common finding of a genuine DPIA is "
            "that two or three fields are collected because they were in the "
            "form template, and removing them is cheaper than protecting them."
        ),
        required=True,
        reportable=True,
    ),
    _q(
        "alternatives",
        "What less intrusive alternatives did you consider, and why were they rejected?",
        section="Necessity and proportionality",
        helper=(
            "Aggregation, pseudonymisation, sampling, shorter retention, or "
            "asking the person rather than inferring. 'None' is a legitimate "
            "answer only if it is true."
        ),
        reportable=True,
    ),

    # ---- transfers ------------------------------------------------------ #
    _q(
        "transfers_outside_india",
        "Will personal data be transferred or stored outside India?",
        type="boolean",
        section="Transfers and processors",
        helper=(
            "§16 lets the Central Government restrict transfers to notified "
            "countries. Cloud storage in another region counts as a transfer, "
            "and so does support access from an overseas team."
        ),
        required=True,
        reportable=True,
    ),
    _q(
        "transfer_countries",
        "Which countries, and what is the arrangement?",
        section="Transfers and processors",
        helper="Name the countries and the contractual basis for each.",
        show_if={"key": "transfers_outside_india", "equals": True},
        reportable=True,
    ),
    _q(
        "processors",
        "Which processors and sub-processors will handle this data?",
        section="Transfers and processors",
        helper=(
            "§8(2) keeps you responsible for your processors. List them, and say "
            "whether each is covered by a contract that requires them to act on "
            "a rights request you pass on."
        ),
        required=True,
        reportable=True,
    ),

    # ---- risks and what will be done about them ------------------------- #
    _q(
        "risks",
        "What could go wrong, and who would be harmed?",
        section="Risks",
        helper=(
            "Think about the person, not the company. Unauthorised access, "
            "accidental disclosure, inaccurate data driving a wrong decision, "
            "data kept long past its purpose, a processor's breach, or an "
            "inability to answer a rights request because nobody knows where "
            "the data is."
        ),
        required=True,
        reportable=True,
    ),
    _q(
        "risk_level",
        "Overall, how would you rate the residual risk?",
        type="single_choice",
        section="Risks",
        options=["Low", "Medium", "High"],
        required=True,
        reportable=True,
    ),
    _q(
        "mitigations",
        "What will you do to reduce each risk, and who is doing it by when?",
        section="Risks",
        helper=(
            "This is the part that changes anything. A DPIA that identifies "
            "risks and assigns nothing has documented a process and decided "
            "nothing. Name the person and the date."
        ),
        required=True,
        reportable=True,
    ),
    _q(
        "security_measures",
        "What security safeguards apply? (§8(5))",
        type="multi_choice",
        section="Risks",
        options=[
            "Encryption at rest",
            "Encryption in transit",
            "Access control by role",
            "Audit logging of access",
            "Pseudonymisation or tokenisation",
            "Backup and tested restore",
            "Vendor security review",
            "Staff training",
        ],
        required=True,
        reportable=True,
    ),
    _q(
        "residual_accepted_by",
        "Who has accepted the residual risk?",
        type="text",
        section="Risks",
        helper=(
            "A named person, not a team. Accepting risk on behalf of the people "
            "whose data it is should have somebody's name against it."
        ),
        reportable=True,
    ),
    _q(
        "evidence",
        "Attach any supporting documents.",
        type="evidence",
        section="Risks",
        helper=(
            "Data flow diagrams, the notice text, the processor contract, the "
            "security review."
        ),
    ),
]


# --------------------------------------------------------------------------- #
# RoPA
# --------------------------------------------------------------------------- #

ROPA: list[Q] = [
    _q("activity_name", "What is this processing activity called?",
       type="text", section="The activity", required=True, reportable=True,
       helper="A name somebody in the business would recognise."),
    _q("business_owner", "Which team or person owns it?",
       type="text", section="The activity", required=True, reportable=True),
    _q("purpose", "What is the purpose?",
       section="The activity", required=True, reportable=True,
       helper=(
           "One activity, one purpose. If you find yourself writing 'and', it "
           "is probably two activities — and the distinction matters, because "
           "consent and retention attach to a purpose."
       )),
    _q("lawful_basis", "On what basis is it lawful?",
       type="single_choice", section="The activity", required=True,
       reportable=True,
       options=[
           "Consent (§6)",
           "Voluntarily provided (§7(a))",
           "State subsidy, benefit, service or licence (§7(b))",
           "Function of the State (§7(c)-(d))",
           "Legal obligation or court order (§7(e)-(f))",
           "Medical emergency (§7(g)-(h))",
           "Disaster or public order (§7(i))",
           "Employment (§7(j))",
       ]),
    _q("data_subjects", "Whose data is it?",
       type="multi_choice", section="The data", required=True, reportable=True,
       options=["Customers", "Prospects", "Employees", "Job applicants",
                "Suppliers' staff", "Children", "Website visitors", "Other"]),
    _q("data_categories", "Which categories of personal data?",
       type="multi_choice", section="The data", required=True, reportable=True,
       options=["Name and contact details",
                "Government identifiers (Aadhaar, PAN, passport, voter ID)",
                "Financial data", "Health data", "Biometric data",
                "Location data", "Behavioural or usage data",
                "Employment or education records", "Other"]),
    _q("systems", "Which systems hold it?",
       section="The data", required=True, reportable=True,
       helper=(
           "Name the actual systems, including the ones this product has no "
           "connection to — spreadsheets, an archive, a processor's own "
           "database. The systems nobody lists are the ones a rights request "
           "misses."
       )),
    _q("recipients", "Who receives it, inside and outside the organisation?",
       section="Sharing", required=True, reportable=True),
    _q("transfers_outside_india", "Is any of it transferred outside India?",
       type="boolean", section="Sharing", required=True, reportable=True),
    _q("transfer_detail", "Where to, and under what arrangement?",
       section="Sharing", reportable=True,
       show_if={"key": "transfers_outside_india", "equals": True}),
    _q("retention", "How long is it kept, and what triggers deletion?",
       section="Retention", required=True, reportable=True,
       helper=(
           "Say what starts the clock and what actually performs the deletion. "
           "A retention period nothing enforces is an intention."
       )),
    _q("retention_obligation",
       "Is there a legal obligation to keep it for a set period?",
       type="text", section="Retention", reportable=True,
       helper=(
           "If so, name it. This is the field an erasure request will be "
           "refused on, and a refusal needs a ground."
       )),
    _q("safeguards", "What safeguards apply?",
       type="multi_choice", section="Safeguards", required=True, reportable=True,
       options=["Encryption at rest", "Encryption in transit",
                "Access control by role", "Audit logging",
                "Pseudonymisation", "Backup and tested restore",
                "Data processing agreement in place"]),
]


# --------------------------------------------------------------------------- #
# Vendor privacy — the one that feeds third-party risk
# --------------------------------------------------------------------------- #

VENDOR: list[Q] = [
    _q("has_policy", "Does the vendor publish a privacy policy?",
       type="boolean", section="Governance", required=True, reportable=True),
    _q("dpo", "Do they have a named person responsible for data protection?",
       type="boolean", section="Governance", required=True, reportable=True),
    _q("certifications", "Which certifications do they hold?",
       type="multi_choice", section="Governance", reportable=True,
       options=["ISO 27001", "ISO 27701", "SOC 2 Type II", "PCI DSS",
                "HIPAA", "None", "Other"]),
    _q("laws", "Which data protection laws do they say they comply with?",
       type="text", section="Governance", reportable=True),
    _q("dpa_signed", "Is a data processing agreement in place?",
       type="boolean", section="Contract", required=True, reportable=True,
       helper=(
           "§8(2) makes you responsible for their processing. Without a "
           "contract that obliges them to act on a rights request you pass on, "
           "you have an obligation you cannot discharge."
       )),
    _q("subprocessors", "Do they use sub-processors, and are they disclosed?",
       section="Contract", required=True, reportable=True),
    _q("dsar_support",
       "Will they action an access or erasure request within your deadline?",
       type="boolean", section="Rights", required=True, reportable=True,
       helper=(
           "The statutory clock runs on you, not on them. A processor who takes "
           "45 days is a processor who makes you late."
       )),
    _q("dsar_route", "How is such a request passed to them, and to whom?",
       type="text", section="Rights", reportable=True,
       helper="An address and a method. This becomes the third-party contact on "
              "an action item."),
    _q("breach_notification",
       "How quickly do they undertake to tell you about a breach?",
       type="text", section="Security", required=True, reportable=True,
       helper=(
           "§8(6) requires you to notify the Board and affected persons. You "
           "cannot do that faster than your processor tells you."
       )),
    _q("breach_history",
       "Have they had a publicly reported breach in the last three years?",
       type="boolean", section="Security", reportable=True),
    _q("data_location", "Where is the data stored?",
       type="text", section="Security", required=True, reportable=True),
    _q("encryption", "Is data encrypted at rest and in transit?",
       type="boolean", section="Security", required=True, reportable=True),
    _q("evidence", "Attach their documentation.",
       type="evidence", section="Security",
       helper="Policy, DPA, certification, penetration test summary."),
]


# --------------------------------------------------------------------------- #
# Application discovery — populates the data map from humans
# --------------------------------------------------------------------------- #

DISCOVERY: list[Q] = [
    _q("system_name", "What is the system called?",
       type="text", required=True, reportable=True),
    _q("vendor", "Who supplies it?",
       type="text", reportable=True,
       helper="Leave blank if it was built in-house."),
    _q("what_for", "What does your team use it for?",
       required=True, reportable=True),
    _q("holds_personal_data", "Does it hold personal data about anybody?",
       type="boolean", required=True, reportable=True,
       helper=(
           "Including staff. A system holding only employee records is still "
           "in scope — the Act does not stop applying to somebody because they "
           "work here."
       )),
    _q("whose_data", "Whose?",
       type="multi_choice", reportable=True,
       options=["Customers", "Prospects", "Employees", "Job applicants",
                "Children", "Other"],
       show_if={"key": "holds_personal_data", "equals": True}),
    _q("categories", "What kind of personal data?",
       type="multi_choice", reportable=True,
       options=["Name and contact details", "Government identifiers",
                "Financial data", "Health data", "Location data",
                "Usage or behavioural data", "Other"],
       show_if={"key": "holds_personal_data", "equals": True}),
    _q("who_can_delete",
       "Who in your team can find and delete one person's records in it?",
       type="text", reportable=True,
       show_if={"key": "holds_personal_data", "equals": True},
       helper=(
           "A name. This is the question that decides whether a rights request "
           "against this system is answerable at all."
       )),
    _q("exports_elsewhere", "Does it send data to any other system?",
       reportable=True,
       show_if={"key": "holds_personal_data", "equals": True}),
]


#: Everything shipped, keyed by slug.
BUILT_IN: dict[str, dict[str, Any]] = {
    "dpia": {
        "kind": "dpia",
        "name": "Data Protection Impact Assessment (DPDP §10)",
        "description": (
            "For processing that needs assessing before it starts. Required of a "
            "Significant Data Fiduciary, and useful to anybody."
        ),
        "questions": DPIA,
    },
    "ropa": {
        "kind": "ropa",
        "name": "Record of Processing Activities",
        "description": (
            "One record per processing activity. The document a regulator asks "
            "for first."
        ),
        "questions": ROPA,
    },
    "vendor-privacy": {
        "kind": "vendor",
        "name": "Vendor privacy and security review",
        "description": (
            "For a processor before you send them personal data, and again when "
            "the review falls due."
        ),
        "questions": VENDOR,
    },
    "app-discovery": {
        "kind": "discovery",
        "name": "Application discovery survey",
        "description": (
            "Sent to teams to find the systems nobody has told you about. The "
            "systems that are not on a list are the ones a rights request "
            "misses."
        ),
        "questions": DISCOVERY,
    },
}
