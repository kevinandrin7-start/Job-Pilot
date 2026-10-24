"""Adzuna public API (free key from https://developer.adzuna.com/)."""

from __future__ import annotations

from .base import JobPosting, JobSource, SearchQuery, html_to_text, looks_remote, register


@register
class AdzunaSource(JobSource):
    name = "adzuna"
    label = "Adzuna"
    API_URL = "https://api.adzuna.com/v1/api/jobs/{country}/search/1"

    def enabled(self) -> bool:
        return self.settings.adzuna_configured

    def disabled_reason(self) -> str:
        return "" if self.enabled() else "Add ADZUNA_APP_ID and ADZUNA_APP_KEY to .env to enable."

    def search(self, query: SearchQuery) -> list[JobPosting]:
        params = {
            "app_id": self.settings.adzuna_app_id,
            "app_key": self.settings.adzuna_app_key,
            "what": query.keywords + (" remote" if query.remote_only else ""),
            "results_per_page": min(query.limit, 50),
            "content-type": "application/json",
        }
        if query.location and not query.remote_only:
            params["where"] = query.location
        if query.min_salary:
            params["salary_min"] = int(query.min_salary)
        resp = self.client.get(self.API_URL.format(country=self.settings.adzuna_country), params=params)
        resp.raise_for_status()
        jobs = []
        for item in resp.json().get("results", []):
            title = html_to_text(item.get("title") or "")
            location = (item.get("location") or {}).get("display_name") or ""
            description = html_to_text(item.get("description") or "")
            smin, smax = item.get("salary_min"), item.get("salary_max")
            salary_text = ""
            if smin or smax:
                salary_text = f"{int(smin or smax):,} - {int(smax or smin):,}"
                if item.get("salary_is_predicted") in ("1", 1):
                    salary_text += " (Adzuna estimate)"
            jobs.append(JobPosting(
                source=self.name,
                external_id=str(item.get("id")),
                title=title,
                company=(item.get("company") or {}).get("display_name") or "",
                location=location,
                url=item.get("redirect_url") or "",
                # Adzuna only returns a snippet; import the full posting for better tailoring.
                description=description,
                remote=looks_remote(title, location, description),
                salary_min=smin, salary_max=smax, salary_text=salary_text,
                employment_type=" ".join(x for x in (item.get("contract_time"), item.get("contract_type")) if x)
                    .replace("_", " "),
                posted_at=item.get("created") or "",
                extra={"snippet_only": True},
            ))
        return jobs
