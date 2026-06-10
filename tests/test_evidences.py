"""Evidence endpoint authorization tests.

These lock in the org-membership gate added to the evidence routes: a user
who is not a member of the settlement's organization must not be able to
upload, list, read, or mutate its evidence — being ADMIN of an unrelated
signup-org is not sufficient (mirrors the bank-statement auth tests).
"""

from __future__ import annotations

import io
import uuid

from httpx import AsyncClient

from conftest import (
    add_treasurer_to_org,
    auth_headers,
    create_org_as_admin,
    signup,
)

# The upload route stores bytes without decoding the image, so any non-empty
# payload with an allowed extension is enough to exercise the auth path.
_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"0" * 32


async def _create_settlement(
    client: AsyncClient, headers: dict[str, str], *, org_id: str
) -> dict:
    resp = await client.post(
        f"/api/v1/organizations/{org_id}/settlements",
        headers=headers,
        json={"title": "증빙 권한 테스트", "academic_year": 2026, "semester": "1"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _upload_evidence(
    client: AsyncClient, headers: dict[str, str], *, settlement_id: str
):
    return await client.post(
        f"/api/v1/settlements/{settlement_id}/evidences",
        headers=headers,
        data={"evidence_type": "physical_receipt"},
        files={"file": ("receipt.png", io.BytesIO(_PNG_BYTES), "image/png")},
    )


# --- Happy path ----------------------------------------------------------


async def test_member_can_upload_and_read_evidence(client: AsyncClient) -> None:
    await signup(client, email="ev_admin@konkuk.ac.kr")
    admin_headers = await auth_headers(client, "ev_admin@konkuk.ac.kr")
    org = await create_org_as_admin(client, admin_headers)
    treasurer_headers = await add_treasurer_to_org(
        client,
        org_id=org["id"],
        admin_headers=admin_headers,
        treasurer_email="ev_treasurer@konkuk.ac.kr",
    )
    settlement = await _create_settlement(client, admin_headers, org_id=org["id"])

    up = await _upload_evidence(
        client, treasurer_headers, settlement_id=settlement["id"]
    )
    assert up.status_code == 201, up.text
    evidence_id = up.json()["id"]

    detail = await client.get(
        f"/api/v1/evidences/{evidence_id}", headers=treasurer_headers
    )
    assert detail.status_code == 200, detail.text

    listing = await client.get(
        f"/api/v1/settlements/{settlement['id']}/evidences",
        headers=treasurer_headers,
    )
    assert listing.status_code == 200, listing.text
    assert len(listing.json()) == 1


# --- Authorization -------------------------------------------------------


async def test_non_member_cannot_upload_evidence(client: AsyncClient) -> None:
    await signup(client, email="ev_owner@konkuk.ac.kr")
    owner_headers = await auth_headers(client, "ev_owner@konkuk.ac.kr")
    org = await create_org_as_admin(client, owner_headers)
    settlement = await _create_settlement(client, owner_headers, org_id=org["id"])

    # A different user who is ADMIN of their own signup-org but not a member here.
    await signup(client, email="ev_outsider@konkuk.ac.kr")
    out_headers = await auth_headers(client, "ev_outsider@konkuk.ac.kr")

    resp = await _upload_evidence(client, out_headers, settlement_id=settlement["id"])
    assert resp.status_code == 403, resp.text


async def test_non_member_cannot_list_or_read_evidence(client: AsyncClient) -> None:
    await signup(client, email="ev_owner2@konkuk.ac.kr")
    owner_headers = await auth_headers(client, "ev_owner2@konkuk.ac.kr")
    org = await create_org_as_admin(client, owner_headers)
    settlement = await _create_settlement(client, owner_headers, org_id=org["id"])
    up = await _upload_evidence(client, owner_headers, settlement_id=settlement["id"])
    assert up.status_code == 201, up.text
    evidence_id = up.json()["id"]

    await signup(client, email="ev_outsider2@konkuk.ac.kr")
    out_headers = await auth_headers(client, "ev_outsider2@konkuk.ac.kr")

    listing = await client.get(
        f"/api/v1/settlements/{settlement['id']}/evidences", headers=out_headers
    )
    assert listing.status_code == 403, listing.text

    detail = await client.get(
        f"/api/v1/evidences/{evidence_id}", headers=out_headers
    )
    assert detail.status_code == 403, detail.text


async def test_non_member_cannot_patch_evidence(client: AsyncClient) -> None:
    await signup(client, email="ev_owner3@konkuk.ac.kr")
    owner_headers = await auth_headers(client, "ev_owner3@konkuk.ac.kr")
    org = await create_org_as_admin(client, owner_headers)
    settlement = await _create_settlement(client, owner_headers, org_id=org["id"])
    up = await _upload_evidence(client, owner_headers, settlement_id=settlement["id"])
    evidence_id = up.json()["id"]

    await signup(client, email="ev_outsider3@konkuk.ac.kr")
    out_headers = await auth_headers(client, "ev_outsider3@konkuk.ac.kr")

    resp = await client.patch(
        f"/api/v1/evidences/{evidence_id}",
        headers=out_headers,
        json={"merchant_name": "무단수정"},
    )
    assert resp.status_code == 403, resp.text


async def test_delete_evidence_removes_row_and_file(client: AsyncClient) -> None:
    await signup(client, email="ev_del@konkuk.ac.kr")
    headers = await auth_headers(client, "ev_del@konkuk.ac.kr")
    org = await create_org_as_admin(client, headers)
    settlement = await _create_settlement(client, headers, org_id=org["id"])

    up = await _upload_evidence(client, headers, settlement_id=settlement["id"])
    assert up.status_code == 201, up.text
    evidence_id = up.json()["id"]

    del_resp = await client.delete(f"/api/v1/evidences/{evidence_id}", headers=headers)
    assert del_resp.status_code == 204, del_resp.text

    listing = await client.get(
        f"/api/v1/settlements/{settlement['id']}/evidences", headers=headers
    )
    assert listing.status_code == 200
    assert listing.json() == []

    detail = await client.get(f"/api/v1/evidences/{evidence_id}", headers=headers)
    assert detail.status_code == 404, detail.text


async def test_non_member_cannot_delete_evidence(client: AsyncClient) -> None:
    await signup(client, email="ev_del_owner@konkuk.ac.kr")
    owner_headers = await auth_headers(client, "ev_del_owner@konkuk.ac.kr")
    org = await create_org_as_admin(client, owner_headers)
    settlement = await _create_settlement(client, owner_headers, org_id=org["id"])
    up = await _upload_evidence(client, owner_headers, settlement_id=settlement["id"])
    evidence_id = up.json()["id"]

    await signup(client, email="ev_del_out@konkuk.ac.kr")
    out_headers = await auth_headers(client, "ev_del_out@konkuk.ac.kr")

    resp = await client.delete(f"/api/v1/evidences/{evidence_id}", headers=out_headers)
    assert resp.status_code == 403, resp.text


async def test_delete_missing_evidence_returns_404(client: AsyncClient) -> None:
    await signup(client, email="ev_del_nf@konkuk.ac.kr")
    headers = await auth_headers(client, "ev_del_nf@konkuk.ac.kr")

    resp = await client.delete(f"/api/v1/evidences/{uuid.uuid4()}", headers=headers)
    assert resp.status_code == 404, resp.text


async def test_upload_to_missing_settlement_returns_404(client: AsyncClient) -> None:
    await signup(client, email="ev_404@konkuk.ac.kr")
    headers = await auth_headers(client, "ev_404@konkuk.ac.kr")
    missing_id = "00000000-0000-0000-0000-000000000000"
    resp = await _upload_evidence(client, headers, settlement_id=missing_id)
    assert resp.status_code == 404, resp.text


async def test_member_can_download_evidence_file(client: AsyncClient) -> None:
    await signup(client, email="evf_admin@konkuk.ac.kr")
    admin_headers = await auth_headers(client, "evf_admin@konkuk.ac.kr")
    org = await create_org_as_admin(client, admin_headers)
    settlement = await _create_settlement(client, admin_headers, org_id=org["id"])
    up = await _upload_evidence(
        client, admin_headers, settlement_id=settlement["id"]
    )
    assert up.status_code == 201, up.text
    evidence_id = up.json()["id"]

    resp = await client.get(
        f"/api/v1/evidences/{evidence_id}/file", headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.content == _PNG_BYTES


async def test_non_member_cannot_download_evidence_file(client: AsyncClient) -> None:
    await signup(client, email="evf_owner@konkuk.ac.kr")
    owner_headers = await auth_headers(client, "evf_owner@konkuk.ac.kr")
    org = await create_org_as_admin(client, owner_headers)
    settlement = await _create_settlement(client, owner_headers, org_id=org["id"])
    up = await _upload_evidence(
        client, owner_headers, settlement_id=settlement["id"]
    )
    evidence_id = up.json()["id"]

    await signup(client, email="evf_outsider@konkuk.ac.kr")
    out_headers = await auth_headers(client, "evf_outsider@konkuk.ac.kr")
    resp = await client.get(
        f"/api/v1/evidences/{evidence_id}/file", headers=out_headers
    )
    assert resp.status_code == 403, resp.text


async def test_cannot_mutate_evidence_after_approval(client: AsyncClient) -> None:
    from conftest import add_auditor_to_org

    await signup(client, email="ev_lock_admin@konkuk.ac.kr")
    admin_headers = await auth_headers(client, "ev_lock_admin@konkuk.ac.kr")
    org = await create_org_as_admin(client, admin_headers)
    auditor_headers = await add_auditor_to_org(
        client,
        org_id=org["id"],
        admin_headers=admin_headers,
        auditor_email="ev_lock_auditor@konkuk.ac.kr",
    )
    settlement = await _create_settlement(client, admin_headers, org_id=org["id"])
    up = await _upload_evidence(
        client, admin_headers, settlement_id=settlement["id"]
    )
    assert up.status_code == 201, up.text
    evidence_id = up.json()["id"]

    submit = await client.post(
        f"/api/v1/settlements/{settlement['id']}/submit", headers=admin_headers
    )
    assert submit.status_code == 200, submit.text
    approve = await client.post(
        f"/api/v1/settlements/{settlement['id']}/audit/approve",
        headers=auditor_headers,
        json={},
    )
    assert approve.status_code == 200, approve.text

    blocked_upload = await _upload_evidence(
        client, admin_headers, settlement_id=settlement["id"]
    )
    assert blocked_upload.status_code == 409, blocked_upload.text

    blocked_patch = await client.patch(
        f"/api/v1/evidences/{evidence_id}",
        headers=admin_headers,
        json={"merchant_name": "승인 후 수정"},
    )
    assert blocked_patch.status_code == 409, blocked_patch.text

    blocked_delete = await client.delete(
        f"/api/v1/evidences/{evidence_id}", headers=admin_headers
    )
    assert blocked_delete.status_code == 409, blocked_delete.text


async def test_rejected_settlement_allows_evidence_upload(client: AsyncClient) -> None:
    from conftest import add_auditor_to_org

    await signup(client, email="ev_rej_admin@konkuk.ac.kr")
    admin_headers = await auth_headers(client, "ev_rej_admin@konkuk.ac.kr")
    org = await create_org_as_admin(client, admin_headers)
    auditor_headers = await add_auditor_to_org(
        client,
        org_id=org["id"],
        admin_headers=admin_headers,
        auditor_email="ev_rej_auditor@konkuk.ac.kr",
    )
    settlement = await _create_settlement(client, admin_headers, org_id=org["id"])

    await client.post(
        f"/api/v1/settlements/{settlement['id']}/submit", headers=admin_headers
    )
    reject = await client.post(
        f"/api/v1/settlements/{settlement['id']}/audit/reject",
        headers=auditor_headers,
        json={"comment": "보완 필요"},
    )
    assert reject.status_code == 200, reject.text

    up = await _upload_evidence(
        client, admin_headers, settlement_id=settlement["id"]
    )
    assert up.status_code == 201, up.text
