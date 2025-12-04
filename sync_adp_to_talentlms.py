#!/usr/bin/env python3
"""
Sync ADP employees to TalentLMS.

This script:
1. Fetches all TalentLMS users
2. Fetches active employees from ADP (optionally filtered by manager)
3. Creates TalentLMS accounts for ADP employees who don't have one yet

Usage:
    # Sync all active employees
    python sync_adp_to_talentlms.py

    # Sync only employees under a specific manager
    python sync_adp_to_talentlms.py --manager manager@company.com
"""

import sys
import argparse
from typing import Dict, List, Set

from config import DOMAIN, API_KEY
from get_adp_info import get_workers, find_worker_by_identifier, is_active_worker
from import_employees import TalentLMSClient
from sync_single_employee import (
    get_work_email,
    worker_first_last,
    worker_full_name,
    COURSE_ID_ONBOARDING,
)


def get_all_talentlms_emails(client: TalentLMSClient) -> Set[str]:
    """
    Fetch all TalentLMS users and return a set of their emails (normalized to lowercase).
    """
    print("Fetching all TalentLMS users...")
    users = client.get_users()
    emails = set()
    for user in users:
        email = user.get("email")
        if email:
            emails.add(email.strip().lower())
    print(f"Found {len(emails)} TalentLMS users")
    return emails


def get_active_adp_workers(manager_identifier: str | None = None) -> List[Dict]:
    """
    Fetch all workers from ADP and filter to active employees.
    If manager_identifier is provided, only return employees under that manager.
    """
    print("Fetching workers from ADP...")
    data = get_workers()
    workers = data.get("workers", [])

    # Filter to active workers only
    active_workers = [w for w in workers if is_active_worker(w)]
    print(f"Found {len(active_workers)} active workers in ADP")

    # If manager filter specified, find that manager's direct and indirect reports
    if manager_identifier:
        manager_worker = find_worker_by_identifier(workers, manager_identifier)
        if not manager_worker:
            print(f"Error: Could not find manager matching '{manager_identifier}'")
            sys.exit(1)

        manager_id = manager_worker.get("associateOID") or (
            manager_worker.get("workerID") or {}
        ).get("idValue")

        if not manager_id:
            print(f"Error: Manager has no associateOID/workerID")
            sys.exit(1)

        print(f"Filtering to employees under {worker_full_name(manager_worker)}...")

        # Build manager hierarchy to find all reports under this manager
        manager_reports = get_all_reports_under_manager(workers, manager_id)
        active_workers = [w for w in active_workers if w in manager_reports]

        print(f"Found {len(active_workers)} active workers under this manager")

    return active_workers


def get_all_reports_under_manager(workers: List[Dict], manager_id: str) -> List[Dict]:
    """
    Recursively get all direct and indirect reports under a manager.
    """
    from get_adp_info import build_org_hierarchy

    manager_map, worker_map = build_org_hierarchy(workers)

    def collect_reports(mgr_id):
        reports = []
        direct_reports = manager_map.get(mgr_id, [])
        for report in direct_reports:
            reports.append(report)
            # Get their associateOID to find their reports
            report_id = report.get("associateOID") or (
                report.get("workerID") or {}
            ).get("idValue")
            if report_id:
                reports.extend(collect_reports(report_id))
        return reports

    return collect_reports(manager_id)


def sync_workers_to_talentlms(
    workers: List[Dict], existing_emails: Set[str], client: TalentLMSClient
) -> None:
    """
    Create TalentLMS accounts for workers who don't have one.
    """
    created_count = 0
    skipped_count = 0
    error_count = 0

    for worker in workers:
        full_name = worker_full_name(worker)
        email = get_work_email(worker)

        if not email:
            print(f"⊘ {full_name}: No work email found, skipping")
            skipped_count += 1
            continue

        email_lower = email.strip().lower()

        # Check if user already exists in TalentLMS
        if email_lower in existing_emails:
            print(f"✓ {full_name} ({email}): Already has TalentLMS account")
            skipped_count += 1
            continue

        # Create new TalentLMS account
        first_name, last_name = worker_first_last(worker)
        if not first_name and not last_name:
            first_name = "Unknown"
            last_name = "User"

        print(f"→ Creating TalentLMS account for {full_name} ({email})...")

        try:
            created = client.create_user(
                first_name=first_name,
                last_name=last_name,
                email=email,
                login=email,
                password="Testpassword1",  # Dummy hardcoded password
            )

            user_id = created.get('id')
            print(f"  ✓ Created user ID {user_id}")

            # Enroll in onboarding course
            if user_id:
                try:
                    client.add_user_to_course(int(user_id), COURSE_ID_ONBOARDING)
                    print(f"  ✓ Enrolled in course {COURSE_ID_ONBOARDING}")
                except Exception as e:
                    print(f"  ⚠ Enrollment failed: {e}")

            created_count += 1

        except Exception as e:
            print(f"  ✗ Error creating user: {e}")
            error_count += 1

    # Summary
    print("\n" + "=" * 60)
    print("SYNC SUMMARY")
    print("=" * 60)
    print(f"Total ADP workers processed: {len(workers)}")
    print(f"New accounts created:        {created_count}")
    print(f"Already had accounts:        {skipped_count}")
    print(f"Errors:                      {error_count}")
    print("=" * 60)


def main():
    """Main execution"""
    parser = argparse.ArgumentParser(
        description="Sync ADP employees to TalentLMS",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Sync all active employees
  python sync_adp_to_talentlms.py

  # Sync only employees under a specific manager
  python sync_adp_to_talentlms.py --manager manager@company.com
        """,
    )
    parser.add_argument(
        "--manager",
        type=str,
        help="Only sync employees under this manager (email, name, or worker ID)",
    )

    args = parser.parse_args()

    # Initialize TalentLMS client
    client = TalentLMSClient(DOMAIN, API_KEY)

    # Step 1: Get all existing TalentLMS users
    existing_emails = get_all_talentlms_emails(client)

    # Step 2: Get active ADP employees (optionally filtered by manager)
    active_workers = get_active_adp_workers(manager_identifier=args.manager)

    if not active_workers:
        print("No active workers found. Nothing to sync.")
        return

    # Step 3: Compare and create missing accounts
    print("\nStarting sync...\n")
    sync_workers_to_talentlms(active_workers, existing_emails, client)


if __name__ == "__main__":
    main()
