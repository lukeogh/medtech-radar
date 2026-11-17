"""Two invented adverts for the advert layout (jobs_page.advert_layout).

STORED is an advert as the page checker stores it (check_jobs._text): one
line per block of the page, no blank lines and no marks for a heading or a
list item. It carries what the owner checks on role 1760 on the box: no pay
figure, so Rate is not stated, and a privacy notice and a scam warning at the
end, with an agency notice, an equal opportunity line and the company's "who
we are" section beside them.

TYPED is an advert typed by hand: blank lines between paragraphs, typed
bullets, numbered steps, a colon heading and an "Equal opportunities"
section.

Both are fiction, in the invented world the test fixtures use; no company,
person or address here is real.
"""

STORED_COMPANY = "Corvane Medical"

STORED = """About the job
Corvane Medical is hiring a Software Quality Lead to take the firmware of its Class IIb ventilator through a notified body audit next spring.
You will own the IEC 62304 software lifecycle for the ventilator firmware and lead a team of four verification engineers.
Responsibilities
Own the software development plan and the software safety classification for the ventilator firmware.
Run ISO 14971 risk management for software hazards.
Prepare the software sections of the technical file for the notified body.
Set the verification strategy for embedded firmware, including hardware-in-the-loop rigs, unit test coverage targets and the release criteria agreed with clinical, regulatory and manufacturing colleagues across both sites.
Report software quality status to the Head of Quality each month.
What you will bring
Software quality leadership on Class IIb or Class III devices
Working knowledge of IEC 60601-1 and IEC 62366-1
Experience of a notified body audit
Location: Harrowgate Park, two days a week on site
Benefits
Pension Scheme
Private Health Insurance
25 days holiday plus bank holidays
About Corvane Medical
Corvane Medical designs ventilators and patient monitors for intensive care units. Founded in 2009, we are 210 people across two sites, and our devices are used in more than 40 countries.
Corvane Medical is an equal opportunity employer. We welcome applications from everyone, regardless of age, disability, gender, race, religion or sexual orientation.
Privacy notice: by applying you agree that we will process your personal data for the purpose of this recruitment, as set out in our candidate privacy policy.
Beware of recruitment scams. Corvane Medical will never ask you for payment, bank details or identity documents by text message or through a messaging app.
We do not accept unsolicited CVs from recruitment agencies, and will pay no fee for any candidate an agency introduces."""

TYPED_COMPANY = "Halberd Diagnostics"

TYPED = """The role

Halberd Diagnostics is looking for an interim IEC 62304 lead for its point-of-care analyser, for six months from February.

What you will do:

- Review the software development plan against IEC 62304 class B.
- Close the open findings from the last audit.
- Coach two engineers new to the standard.

How we hire

1. A thirty-minute call with the Head of Software.
2. A technical interview with two of the team.
3. An offer within a week.

Equal opportunities

Halberd Diagnostics values a diverse team and an inclusive workplace. Tell us if you need any reasonable adjustments for the interview."""
