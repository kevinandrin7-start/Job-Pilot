"""Remotive public API (no key). https://remotive.com/api-documentation"""

from __future__ import annotations

from .base import JobPosting, JobSource, SearchQuery, html_to_text, parse_salary, register


@register
class RemotiveSource(JobSource):
    name = "remotive"
    label = "Remotive (remote jobs)"
    API_URL = "https://remotive.com/api/remote-jobs"
    min_interval_seconds = 30  # Remotive asks API users to keep request volume low

    def search(self, query: SearchQuery) -> list[JobPosting]:
        resp = self.client.get(self.API_URL, params={"search": query.keywords, "limit": query.limit})
        resp.raise_for_status()
        jobs = []
        for item in resp.json().get("jobs", [])[: query.limit]:
            salary_text = item.get("salary") or ""
            smin, smax = parse_salary(salary_text)
            jobs.append(JobPosting(
                source=self.name,
                external_id=str(item.get("id")),
                title=item.get("title") or "",
                company=item.get("company_name") or "",
                location=item.get("candidate_required_location") or "Remote",
                url=item.get("url") or "",
                description=html_to_text(item.get("description") or ""),
                remote=True,
                salary_min=smin, salary_max=smax, salary_text=salary_text,
                employment_type=(item.get("job_type") or "").replace("_", " "),
                posted_at=item.get("publication_date") or "",
            ))
        return jobs
