#!/usr/bin/env python3
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from bson import ObjectId
from flask import Flask, jsonify, request
from flask_cors import CORS
from pymongo import MongoClient


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_id(value: str) -> ObjectId | None:
    try:
        return ObjectId(value)
    except Exception:
        return None


def _serialize_job(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(doc["_id"]),
        "title": doc.get("title", ""),
        "company": doc.get("company", ""),
        "url": doc.get("url", ""),
        "apply_url": doc.get("apply_url") or "",
        "city": doc.get("city") or "",
        "description": doc.get("description", ""),
        "description_html": doc.get("description_html") or "",
    }


def _serialize_application(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(doc["_id"]),
        "job_id": str(doc.get("job_id", "")),
        "appli_time": doc.get("appli_time", ""),
        "cost": doc.get("cost"),
        "duration": doc.get("duration"),
        "fields": doc.get("fields", []),
    }


QUEUE_STATUSES = ("pending", "in_progress", "done", "error")


def _serialize_queue(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(doc["_id"]),
        "job_id": str(doc.get("job_id", "")),
        "profile_id": doc.get("profile_id", ""),
        "priority": int(doc.get("priority", 0) or 0),
        "status": doc.get("status") or "pending",
        "machine_id": doc.get("machine_id") or "",
        "error": doc.get("error") or "",
        "created_at": doc.get("created_at", ""),
        "updated_at": doc.get("updated_at", ""),
        "started_at": doc.get("started_at", ""),
        "finished_at": doc.get("finished_at", ""),
    }


def create_app() -> Flask:
    app = Flask(__name__)
    CORS(app, resources={r"/api/*": {"origins": "*"}})

    # Prefer JOB_BOARD_* going forward, but keep MARKET_* for compatibility.
    mongo_uri = os.environ.get(
        "JOB_BOARD_MONGO_URI",
        os.environ.get("MARKET_MONGO_URI", "mongodb://localhost:27017"),
    )
    db_name = os.environ.get(
        "JOB_BOARD_DB_NAME",
        os.environ.get("MARKET_DB_NAME", "job_board_db"),
    )
    client = MongoClient(mongo_uri)
    db = client[db_name]

    jobs = db["jobs"]
    applications = db["applications"]
    queue = db["queue"]

    jobs.create_index("url")
    applications.create_index("job_id")
    queue.create_index("job_id")
    queue.create_index([("status", 1), ("priority", -1)])

    @app.get("/api/health")
    def health():
        try:
            client.admin.command("ping")
            mongo_ok = True
        except Exception:
            mongo_ok = False
        return jsonify({"ok": True, "mongo": mongo_ok, "db": db_name})

    @app.get("/api/jobs")
    def list_jobs():
        rows = [_serialize_job(x) for x in jobs.find().sort("_id", -1)]
        return jsonify(rows)

    @app.post("/api/jobs")
    def create_job():
        body = request.get_json(silent=True) or {}
        title = (body.get("title") or "").strip()
        company = (body.get("company") or "").strip()
        url = (body.get("url") or "").strip()
        description = (body.get("description") or "").strip()
        if not title or not company or not url:
            return jsonify({"error": "title, company and url are required"}), 400

        inserted = jobs.insert_one(
            {
                "title": title,
                "company": company,
                "url": url,
                "description": description,
                "created_at": _utc_iso(),
            }
        )
        row = jobs.find_one({"_id": inserted.inserted_id})
        return jsonify(_serialize_job(row)), 201

    @app.get("/api/jobs/<job_id>")
    def get_job(job_id: str):
        oid = _to_id(job_id)
        if not oid:
            return jsonify({"error": "invalid job id"}), 400
        row = jobs.find_one({"_id": oid})
        if not row:
            return jsonify({"error": "not found"}), 404
        return jsonify(_serialize_job(row))

    @app.delete("/api/jobs/<job_id>")
    def delete_job(job_id: str):
        oid = _to_id(job_id)
        if not oid:
            return jsonify({"error": "invalid job id"}), 400
        deleted = jobs.delete_one({"_id": oid})
        if deleted.deleted_count == 0:
            return jsonify({"error": "not found"}), 404
        applications.delete_many({"job_id": str(oid)})
        return jsonify({"ok": True})

    @app.get("/api/applications")
    def list_applications():
        job_id = (request.args.get("job_id") or "").strip()
        query: dict[str, Any] = {}
        if job_id:
            query["job_id"] = job_id
        rows = [_serialize_application(x) for x in applications.find(query).sort("_id", -1)]
        return jsonify(rows)

    @app.post("/api/applications")
    def create_application():
        body = request.get_json(silent=True) or {}
        job_id = (body.get("job_id") or "").strip()
        appli_time = (body.get("appli_time") or "").strip() or _utc_iso()
        cost = body.get("cost")
        duration = body.get("duration")
        fields = body.get("fields")
        if not isinstance(fields, list):
            fields = []

        oid = _to_id(job_id)
        if not oid:
            return jsonify({"error": "invalid job_id"}), 400
        if not jobs.find_one({"_id": oid}):
            return jsonify({"error": "job not found"}), 404

        for item in fields:
            if not isinstance(item, dict) or "key" not in item or "value" not in item:
                return jsonify({"error": "fields must be an array of {key, value}"}), 400

        inserted = applications.insert_one(
            {
                "job_id": job_id,
                "appli_time": appli_time,
                "cost": cost,
                "duration": duration,
                "fields": fields,
                "created_at": _utc_iso(),
            }
        )
        row = applications.find_one({"_id": inserted.inserted_id})
        return jsonify(_serialize_application(row)), 201

    # ---------------------- Queue endpoints ---------------------------------

    @app.get("/api/queue")
    def list_queue():
        status = (request.args.get("status") or "").strip()
        query: dict[str, Any] = {}
        if status:
            if status not in QUEUE_STATUSES:
                return jsonify({"error": f"invalid status: {status}"}), 400
            query["status"] = status
        rows = list(
            queue.find(query).sort(
                [("status", 1), ("priority", -1), ("created_at", 1)]
            )
        )
        return jsonify([_serialize_queue(r) for r in rows])

    @app.post("/api/queue")
    def create_queue_item():
        body = request.get_json(silent=True) or {}
        job_id = (body.get("job_id") or "").strip()
        profile_id = (body.get("profile_id") or "").strip()
        if not profile_id:
            profile_id = (os.environ.get("JOB_BOARD_DEFAULT_PROFILE_ID") or "main").strip()
        try:
            priority = int(body.get("priority", 0) or 0)
        except (TypeError, ValueError):
            return jsonify({"error": "priority must be an integer"}), 400

        oid = _to_id(job_id)
        if not oid:
            return jsonify({"error": "invalid job_id"}), 400
        if not jobs.find_one({"_id": oid}):
            return jsonify({"error": "job not found"}), 404

        now = _utc_iso()
        inserted = queue.insert_one(
            {
                "job_id": job_id,
                "profile_id": profile_id,
                "priority": priority,
                "status": "pending",
                "machine_id": "",
                "error": "",
                "created_at": now,
                "updated_at": now,
                "started_at": "",
                "finished_at": "",
            }
        )
        row = queue.find_one({"_id": inserted.inserted_id})
        return jsonify(_serialize_queue(row)), 201

    @app.patch("/api/queue/<qid>")
    def update_queue_item(qid: str):
        oid = _to_id(qid)
        if not oid:
            return jsonify({"error": "invalid queue id"}), 400
        body = request.get_json(silent=True) or {}
        update: dict[str, Any] = {}

        if "priority" in body:
            try:
                update["priority"] = int(body["priority"])
            except (TypeError, ValueError):
                return jsonify({"error": "priority must be an integer"}), 400

        if "status" in body:
            status = (body.get("status") or "").strip()
            if status not in QUEUE_STATUSES:
                return jsonify({"error": f"invalid status: {status}"}), 400
            update["status"] = status
            now = _utc_iso()
            if status == "in_progress":
                update["started_at"] = now
            if status in ("done", "error"):
                update["finished_at"] = now

        if "machine_id" in body:
            update["machine_id"] = (body.get("machine_id") or "").strip()

        if "error" in body:
            update["error"] = (body.get("error") or "").strip()

        if "profile_id" in body:
            update["profile_id"] = (body.get("profile_id") or "").strip()

        if not update:
            return jsonify({"error": "no updatable fields provided"}), 400

        update["updated_at"] = _utc_iso()
        res = queue.update_one({"_id": oid}, {"$set": update})
        if res.matched_count == 0:
            return jsonify({"error": "not found"}), 404
        row = queue.find_one({"_id": oid})
        return jsonify(_serialize_queue(row))

    @app.delete("/api/queue/<qid>")
    def delete_queue_item(qid: str):
        oid = _to_id(qid)
        if not oid:
            return jsonify({"error": "invalid queue id"}), 400
        res = queue.delete_one({"_id": oid})
        if res.deleted_count == 0:
            return jsonify({"error": "not found"}), 404
        return jsonify({"ok": True})

    return app


app = create_app()


if __name__ == "__main__":
    port = int(os.environ.get("JOB_BOARD_PORT", os.environ.get("MARKET_PORT", "5060")))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG") == "1")
