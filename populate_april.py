#!/usr/bin/env python3
"""Script para popular banco local com dados ficticios de abril/2026 para testes."""

from app import app, db, Collaborator, HourEntry
from datetime import date, timedelta
from decimal import Decimal

def populate_april_2026():
    """Popula abril/2026 com lançamentos fictícios."""
    with app.app_context():
        # Pega o único colaborador ou cria um novo
        collab = Collaborator.query.first()
        if not collab:
            collab = Collaborator(
                name="Luciano",
                role="Açougueiro",
                daily_rate=Decimal("150.00"),
                active=True,
            )
            db.session.add(collab)
            db.session.commit()

        print(f"Usando colaborador: {collab.name} (ID={collab.id})")

        # Limpa lançamentos de abril que já existem
        HourEntry.query.filter(
            HourEntry.entry_date >= date(2026, 4, 1),
            HourEntry.entry_date < date(2026, 5, 1),
        ).delete()
        db.session.commit()

        # Gera dados fictícios para abril
        # Padrão: 8h de seg-sex, repouso sábado, 4h domingo
        entries = []
        april_start = date(2026, 4, 1)
        for day_offset in range(30):  # 30 dias de abril
            current_date = april_start + timedelta(days=day_offset)
            weekday = current_date.weekday()  # 0=seg, 6=dom

            hours = None
            note = None
            if weekday == 5:  # sábado
                hours = Decimal("0")
                note = "Repouso"
            elif weekday == 6:  # domingo
                hours = Decimal("4")
                note = "Domingo"
            else:  # seg-sex
                hours = Decimal("8")

            if hours is not None:
                entry = HourEntry(
                    collaborator_id=collab.id,
                    entry_date=current_date,
                    hours=hours,
                    note=note,
                    archived=False,
                )
                entries.append(entry)

        db.session.bulk_save_objects(entries)
        db.session.commit()
        print(f"✓ Criados {len(entries)} lançamentos em abril/2026")

        # Sumário
        pos_hours = db.session.query(
            db.func.sum(HourEntry.hours)
        ).filter(
            HourEntry.collaborator_id == collab.id,
            HourEntry.entry_date >= date(2026, 4, 1),
            HourEntry.entry_date < date(2026, 5, 1),
            HourEntry.hours > 0,
        ).scalar() or 0

        neg_hours = db.session.query(
            db.func.sum(HourEntry.hours)
        ).filter(
            HourEntry.collaborator_id == collab.id,
            HourEntry.entry_date >= date(2026, 4, 1),
            HourEntry.entry_date < date(2026, 5, 1),
            HourEntry.hours < 0,
        ).scalar() or 0

        net = float(pos_hours) - float(abs(neg_hours))
        print(f"Resumo abril:")
        print(f"  Horas positivas: {float(pos_hours):.2f}")
        print(f"  Horas negativas: {float(abs(neg_hours)):.2f}")
        print(f"  Horas líquidas: {net:.2f}")

if __name__ == "__main__":
    populate_april_2026()
