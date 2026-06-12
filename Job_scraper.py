
from __future__ import annotations
import argparse
import os
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote_plus, urljoin

try:
    import pandas as pd
    from bs4 import BeautifulSoup
    from selenium import webdriver
    from selenium.common.exceptions import TimeoutException, WebDriverException
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait
except ImportError:
    sys.exit(
        "Missing dependencies. Activate the virtual environment and install packages:\n"
        "  .\\.venv\\Scripts\\Activate.ps1\n"
        "  pip install -r requirements.txt"
    )

LINKEDIN_GUEST_API = (
    "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
)
PAGE_OFFSET = 25
DEFAULT_SCHEDULE_TIME = "21:00"
TASK_NAME = "LinkedInJobScraper"


@dataclass
class JobListing:
    title: str
    company: str
    location: str
    posted: str
    url: str
    description: str = ""


def _normalize_job_url(url: str) -> str:
    url = urljoin("https://www.linkedin.com", url.split("?")[0])
    match = re.search(r"/jobs/view/(?:[^/]+-)?(\d+)", url)
    if match:
        return f"https://www.linkedin.com/jobs/view/{match.group(1)}"
    return url


def _job_id(url: str) -> str:
    match = re.search(r"/jobs/view/(?:[^/]+-)?(\d+)", url)
    return match.group(1) if match else url


class LinkedInJobScraper:
    def __init__(self, headless: bool = True, wait_seconds: int = 15):
        self.wait_seconds = wait_seconds
        self.driver: webdriver.Chrome | None = None
        self.driver = self._create_driver(headless=headless)

    def _create_driver(self, headless: bool) -> webdriver.Chrome:
        opts = Options()
        if headless:
            opts.add_argument("--headless=new")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--disable-blink-features=AutomationControlled")
        opts.add_experimental_option("excludeSwitches", ["enable-automation"])
        opts.add_experimental_option("useAutomationExtension", False)
        opts.add_argument(
            "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        try:
            driver = webdriver.Chrome(options=opts)
        except WebDriverException as exc:
            raise SystemExit(
                "Could not start Chrome. Install Google Chrome and try again."
            ) from exc
        driver.set_window_size(1400, 900)
        return driver

    def close(self) -> None:
        if self.driver:
            self.driver.quit()
            self.driver = None

    def login(self, email: str, password: str) -> None:
        assert self.driver is not None
        self.driver.get("https://www.linkedin.com/login")
        wait = WebDriverWait(self.driver, self.wait_seconds)

        email_input = wait.until(EC.presence_of_element_located((By.ID, "username")))
        password_input = wait.until(EC.presence_of_element_located((By.ID, "password")))

        email_input.clear()
        email_input.send_keys(email)
        password_input.clear()
        password_input.send_keys(password)

        self.driver.find_element(By.CSS_SELECTOR, "button[type='submit']").click()

        try:
            wait.until(
                EC.any_of(
                    EC.url_contains("linkedin.com/feed"),
                    EC.url_contains("linkedin.com/jobs"),
                    EC.url_contains("linkedin.com/mynetwork"),
                )
            )
        except TimeoutException as exc:
            current_url = self.driver.current_url
            if "checkpoint" in current_url or "challenge" in current_url:
                raise RuntimeError(
                    "LinkedIn requires additional verification (CAPTCHA/2FA). "
                    "Run with --no-headless and complete verification manually."
                ) from exc
            raise RuntimeError(
                f"Login failed. LinkedIn redirected to: {current_url}"
            ) from exc

        time.sleep(2)

    def _build_search_url(
        self,
        keywords: str,
        location: str,
        start: int = 0,
        remote: bool = False,
    ) -> str:
        params = [
            f"keywords={quote_plus(keywords)}",
            f"location={quote_plus(location)}",
            f"start={start}",
        ]
        if remote:
            params.append("f_WT=2")
        return f"{LINKEDIN_GUEST_API}?{'&'.join(params)}"

    def _parse_listing_cards(self, html: str) -> list[JobListing]:
        soup = BeautifulSoup(html, "html.parser")
        jobs: list[JobListing] = []
        seen_ids: set[str] = set()

        for card in soup.select("li"):
            link_tag = card.select_one("a[href*='/jobs/view/']")
            if not link_tag:
                continue

            title_tag = card.select_one(".base-search-card__title")
            company_tag = card.select_one(".base-search-card__subtitle")
            location_tag = card.select_one(".job-search-card__location")
            posted_tag = card.select_one("time")

            title = title_tag.get_text(strip=True) if title_tag else ""
            url = _normalize_job_url(link_tag.get("href", ""))
            job_key = _job_id(url)

            if not title or not url or job_key in seen_ids:
                continue

            seen_ids.add(job_key)
            jobs.append(
                JobListing(
                    title=title,
                    company=company_tag.get_text(strip=True) if company_tag else "",
                    location=location_tag.get_text(strip=True) if location_tag else "",
                    posted=posted_tag.get_text(strip=True) if posted_tag else "",
                    url=url,
                )
            )

        return jobs

    def _fetch_job_description(self, url: str) -> str:
        assert self.driver is not None
        self.driver.get(url)
        wait = WebDriverWait(self.driver, self.wait_seconds)
        try:
            wait.until(
                EC.presence_of_element_located(
                    (
                        By.CSS_SELECTOR,
                        "div.show-more-less-html__markup, "
                        "div.description__text, "
                        "article.jobs-description__container",
                    )
                )
            )
        except TimeoutException:
            return ""

        soup = BeautifulSoup(self.driver.page_source, "html.parser")
        description = soup.select_one(
            "div.show-more-less-html__markup, div.description__text"
        )
        return description.get_text("\n", strip=True) if description else ""

    def scrape(
        self,
        keywords: str,
        location: str = "",
        max_pages: int = 3,
        fetch_descriptions: bool = False,
        remote: bool = False,
    ) -> list[JobListing]:
        assert self.driver is not None
        all_jobs: list[JobListing] = []
        seen_ids: set[str] = set()
        wait = WebDriverWait(self.driver, self.wait_seconds)

        for page in range(max_pages):
            start = page * PAGE_OFFSET
            url = self._build_search_url(keywords, location, start=start, remote=remote)
            self.driver.get(url)

            try:
                wait.until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "li a[href*='/jobs/view/']"))
                )
            except TimeoutException:
                break

            page_jobs = self._parse_listing_cards(self.driver.page_source)
            new_jobs = [job for job in page_jobs if _job_id(job.url) not in seen_ids]

            if not new_jobs:
                break

            for job in new_jobs:
                seen_ids.add(_job_id(job.url))
                all_jobs.append(job)

            time.sleep(1.5)

        if fetch_descriptions:
            for job in all_jobs:
                job.description = self._fetch_job_description(job.url)
                time.sleep(1.5)

        return all_jobs


def dated_output_path(path: str) -> str:
    stem, ext = os.path.splitext(path)
    if not ext:
        ext = ".csv"
    date_str = datetime.now().strftime("%Y-%m-%d")
    return f"{stem}_{date_str}{ext}"


def parse_schedule_time(value: str) -> tuple[int, int]:
    value = value.strip().lower()
    for fmt in ("%H:%M", "%I:%M%p", "%I%p"):
        try:
            parsed = datetime.strptime(
                value.replace(" ", ""),
                fmt,
            )
            return parsed.hour, parsed.minute
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(
        f"Invalid time '{value}'. Use 24-hour format like 21:00 or 12-hour like 9:00pm."
    )


def seconds_until_next_run(hour: int, minute: int) -> float:
    now = datetime.now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def run_scrape(args: argparse.Namespace) -> int:
    scraper: LinkedInJobScraper | None = None
    email = os.getenv("LINKEDIN_EMAIL")
    password = os.getenv("LINKEDIN_PASSWORD")
    output_path = dated_output_path(args.output) if args.dated else args.output

    try:
        scraper = LinkedInJobScraper(headless=not args.no_headless)

        if email and password:
            scraper.login(email, password)
        elif email or password:
            print("Warning: set both LINKEDIN_EMAIL and LINKEDIN_PASSWORD to log in.")

        jobs = scraper.scrape(
            keywords=args.keywords,
            location=args.location,
            max_pages=args.pages,
            fetch_descriptions=args.descriptions,
            remote=args.remote,
        )

        if not jobs:
            print("No jobs found. Try different keywords, log in, or run with --no-headless.")
            return 0

        df = pd.DataFrame([asdict(job) for job in jobs])
        df.to_csv(output_path, index=False, encoding="utf-8-sig")
        print(f"Saved {len(jobs)} jobs to {output_path}")
        return len(jobs)
    finally:
        if scraper:
            scraper.close()


def run_daily_scheduler(args: argparse.Namespace) -> None:
    hour, minute = parse_schedule_time(args.at)
    print(
        f"Daily scheduler started. Scraper will run every day at {hour:02d}:{minute:02d}."
    )
    print("Leave this window open, or use --install-task for background scheduling.")

    while True:
        wait_seconds = seconds_until_next_run(hour, minute)
        next_run = datetime.now() + timedelta(seconds=wait_seconds)
        print(f"Next run scheduled for {next_run.strftime('%Y-%m-%d %H:%M:%S')}.")
        time.sleep(wait_seconds)

        started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{started}] Starting scheduled scrape...")
        try:
            run_scrape(args)
        except Exception as exc:
            print(f"[{started}] Scheduled scrape failed: {exc}")


def build_task_command(args: argparse.Namespace) -> str:
    project_dir = Path(__file__).resolve().parent
    python_exe = project_dir / ".venv" / "Scripts" / "python.exe"
    script = project_dir / "Job_scrapper.py"

    if not python_exe.exists():
        python_exe = Path(sys.executable)

    command = [
        str(python_exe),
        str(script),
        "-k",
        args.keywords,
        "-l",
        args.location,
        "-p",
        str(args.pages),
        "-o",
        args.output,
    ]
    if args.descriptions:
        command.append("--descriptions")
    if args.remote:
        command.append("--remote")
    if args.dated:
        command.append("--dated")

    return subprocess.list2cmdline(command)


def install_windows_task(args: argparse.Namespace) -> None:
    if sys.platform != "win32":
        raise SystemExit("--install-task is only supported on Windows.")

    hour, minute = parse_schedule_time(args.at)
    task_command = build_task_command(args)

    subprocess.run(
        [
            "schtasks",
            "/Create",
            "/TN",
            TASK_NAME,
            "/TR",
            task_command,
            "/SC",
            "DAILY",
            "/ST",
            f"{hour:02d}:{minute:02d}",
            "/F",
        ],
        check=True,
    )
    print(f"Created Windows scheduled task '{TASK_NAME}' for daily {hour:02d}:{minute:02d}.")
    print(f"Command: {task_command}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scrape LinkedIn job listings.")
    parser.add_argument(
        "-k",
        "--keywords",
        default="Python Developer",
        help="Job search keywords",
    )
    parser.add_argument("-l", "--location", default="United States", help="Job location")
    parser.add_argument("-p", "--pages", type=int, default=3, help="Pages to scrape")
    parser.add_argument(
        "-o",
        "--output",
        default="linkedin_jobs.csv",
        help="Output CSV file path",
    )
    parser.add_argument(
        "--descriptions",
        action="store_true",
        help="Fetch full job descriptions (slower)",
    )
    parser.add_argument(
        "--remote",
        action="store_true",
        help="Filter to remote jobs only",
    )
    parser.add_argument(
        "--no-headless",
        action="store_true",
        help="Show the browser window while scraping",
    )
    parser.add_argument(
        "--schedule",
        action="store_true",
        help="Run the scraper automatically every day at the scheduled time",
    )
    parser.add_argument(
        "--at",
        default=DEFAULT_SCHEDULE_TIME,
        help="Daily run time in 24-hour format (default: 21:00 / 9pm)",
    )
    parser.add_argument(
        "--dated",
        action="store_true",
        help="Append the date to the output CSV filename",
    )
    parser.add_argument(
        "--install-task",
        action="store_true",
        help="Register a Windows Task Scheduler job for daily runs",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.install_task:
        install_windows_task(args)
        return

    if args.schedule:
        run_daily_scheduler(args)
        return

    run_scrape(args)


if __name__ == "__main__":
    main()
