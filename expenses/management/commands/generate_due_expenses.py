from django.core.management.base import BaseCommand
from django.utils import timezone

from expenses.recurrence import generate_due_for_all_businesses


class Command(BaseCommand):
    help = "Generate unpaid expense invoices for recurring expenses due today; historical catch-up requires --include-backlog."

    def add_arguments(self,parser):
        parser.add_argument("--include-backlog",action="store_true",help="Explicit historical catch-up; maximum 100 occurrences per business/run.")

    def handle(self, *args, **options):
        created = generate_due_for_all_businesses(timezone.localdate(),allow_backfill=options["include_backlog"])
        self.stdout.write(self.style.SUCCESS(f"Generated {len(created)} recurring expense(s)."))
