import os
import json
import time
import requests
from pathlib import Path

from flask import Flask, request, jsonify
from flask_cors import CORS
from stellar_sdk import Keypair, Server, TransactionBuilder, Asset
from stellar_sdk.exceptions import BadRequestError, NotFoundError

HORIZON_URL = "https://api.testnet.minepi.com"
NETWORK_PASSPHRASE = "Pi Testnet"
ASSET_CODE = "ALFPay"
ISSUER_PUBLIC = "GBLEWO4ADHHZNEJGVOIVP47VYMAJJ4YWGH7LAO6ZT33VPOZ6ONREGMQO"

PI_API_KEY = os.environ.get("ALFPAY_PI_API_KEY")
PI_PLATFORM_BASE = "https://api.minepi.com/v2"

DISTRIBUTOR_SECRET = os.environ.get("ALFPAY_DISTRIBUTOR_SECRET")
TX_LOG_FILE = Path("transactions_log.json")

PRODUCTS = [
    {"id": "p1", "name": "Bidhaa ya Mfano A", "price_pi": 1, "price_alfpay": 500},
    {"id": "p2", "name": "Bidhaa ya Mfano B", "price_pi": 3, "price_alfpay": 1500},
    {"id": "p3", "name": "Huduma ya Mfano C", "price_pi": 0.5, "price_alfpay": 250},
]

app = Flask(__name__)
CORS(app)

server = Server(horizon_url=HORIZON_URL)
asset = Asset(ASSET_CODE, ISSUER_PUBLIC)


def log_transaction(entry: dict):
    data = []
    if TX_LOG_FILE.exists():
        data = json.loads(TX_LOG_FILE.read_text())
    data.append(entry)
    TX_LOG_FILE.write_text(json.dumps(data, indent=2))


def get_dynamic_fee() -> int:
    try:
        base = server.fetch_base_fee()
    except Exception:
        base = 100
    return max(base * 100, 100000)


@app.route("/api/balance", methods=["GET"])
def balance():
    if not DISTRIBUTOR_SECRET:
        return jsonify({"error": "Server haijawekewa ALFPAY_DISTRIBUTOR_SECRET"}), 500

    distributor_public = Keypair.from_secret(DISTRIBUTOR_SECRET).public_key
    try:
        account_data = server.accounts().account_id(distributor_public).call()
    except NotFoundError:
        return jsonify({"error": "Akaunti haijapatikana kwenye mtandao"}), 404

    for b in account_data["balances"]:
        if b.get("asset_code") == ASSET_CODE:
            return jsonify({
                "asset_code": ASSET_CODE,
                "balance": b["balance"],
                "account": distributor_public,
            })
    return jsonify({"asset_code": ASSET_CODE, "balance": "0", "account": distributor_public})


@app.route("/api/send", methods=["POST"])
def send_payment():
    if not DISTRIBUTOR_SECRET:
        return jsonify({"error": "Server haijawekewa ALFPAY_DISTRIBUTOR_SECRET"}), 500

    body = request.get_json(force=True) or {}
    to_address = body.get("to", "").strip()
    amount = body.get("amount")
    memo = body.get("memo", "")[:28]

    if not to_address or not amount:
        return jsonify({"error": "Tafadhali tuma 'to' na 'amount'"}), 400

    try:
        amount = str(float(amount))
    except ValueError:
        return jsonify({"error": "'amount' si namba sahihi"}), 400

    distributor_kp = Keypair.from_secret(DISTRIBUTOR_SECRET)

    try:
        source_account = server.load_account(distributor_kp.public_key)
        fee = get_dynamic_fee()

        tx_builder = (
            TransactionBuilder(
                source_account=source_account,
                network_passphrase=NETWORK_PASSPHRASE,
                base_fee=fee,
            )
            .append_payment_op(destination=to_address, asset=asset, amount=amount)
            .set_timeout(60)
        )
        if memo:
            tx_builder = tx_builder.add_text_memo(memo)

        tx = tx_builder.build()
        tx.sign(distributor_kp)
        response = server.submit_transaction(tx)

        entry = {
            "type": "send",
            "to": to_address,
            "amount": amount,
            "memo": memo,
            "hash": response.get("hash"),
            "timestamp": time.time(),
        }
        log_transaction(entry)

        return jsonify({"success": True, "hash": response.get("hash"), "amount": amount, "to": to_address})

    except BadRequestError as e:
        return jsonify({"error": "Muamala umeshindwa", "details": str(e)}), 400
    except Exception as e:
        return jsonify({"error": "Hitilafu isiyotarajiwa", "details": str(e)}), 500


@app.route("/api/products", methods=["GET"])
def get_products():
    return jsonify(PRODUCTS)


@app.route("/api/receiving-address", methods=["GET"])
def receiving_address():
    if not DISTRIBUTOR_SECRET:
        return jsonify({"error": "Server haijawekewa ALFPAY_DISTRIBUTOR_SECRET"}), 500
    public_key = Keypair.from_secret(DISTRIBUTOR_SECRET).public_key
    return jsonify({"address": public_key, "asset_code": ASSET_CODE})


@app.route("/api/verify-payment", methods=["POST"])
def verify_payment():
    if not DISTRIBUTOR_SECRET:
        return jsonify({"error": "Server haijawekewa ALFPAY_DISTRIBUTOR_SECRET"}), 500

    body = request.get_json(force=True) or {}
    expected_amount = str(body.get("amount", ""))
    expected_memo = body.get("memo", "")

    if not expected_amount:
        return jsonify({"error": "Tuma 'amount'"}), 400

    distributor_public = Keypair.from_secret(DISTRIBUTOR_SECRET).public_key

    try:
        payments = (
            server.payments()
            .for_account(distributor_public)
            .order(desc=True)
            .limit(20)
            .call()
        )
    except Exception as e:
        return jsonify({"error": "Imeshindwa kusoma miamala", "details": str(e)}), 500

    for record in payments["_embedded"]["records"]:
        if record.get("type") != "payment":
            continue
        if record.get("to") != distributor_public:
            continue
        if record.get("asset_code") != ASSET_CODE:
            continue
        if record.get("amount") != expected_amount:
            continue

        tx_hash = record["transaction_hash"]
        already_used = False
        if TX_LOG_FILE.exists():
            used_hashes = {e.get("hash") for e in json.loads(TX_LOG_FILE.read_text())
                           if e.get("type") == "alfpay_verified"}
            already_used = tx_hash in used_hashes
        if already_used:
            continue

        if expected_memo:
            try:
                tx = server.transactions().transaction(tx_hash).call()
                if tx.get("memo") != expected_memo:
                    continue
            except Exception:
                continue

        log_transaction({
            "type": "alfpay_verified",
            "from": record.get("from"),
            "amount": expected_amount,
            "memo": expected_memo,
            "hash": tx_hash,
            "timestamp": time.time(),
        })
        return jsonify({
            "success": True,
            "hash": tx_hash,
            "from": record.get("from"),
        })

    return jsonify({"success": False, "message": "Malipo hayajaonekana bado. Jaribu tena baada ya sekunde chache."})


@app.route("/payments/approve", methods=["POST"])
def approve_payment():
    if not PI_API_KEY:
        return jsonify({"error": "Server haijawekewa ALFPAY_PI_API_KEY"}), 500

    body = request.get_json(force=True) or {}
    payment_id = body.get("paymentId")
    if not payment_id:
        return jsonify({"error": "Tuma 'paymentId'"}), 400

    resp = requests.post(
        f"{PI_PLATFORM_BASE}/payments/{payment_id}/approve",
        headers={"Authorization": f"Key {PI_API_KEY}"},
    )
    if resp.status_code != 200:
        return jsonify({"error": "Imeshindwa kuidhinisha malipo", "details": resp.text}), 400

    log_transaction({
        "type": "pi_approve",
        "payment_id": payment_id,
        "timestamp": time.time(),
    })
    return jsonify(resp.json())


@app.route("/payments/complete", methods=["POST"])
def complete_payment():
    if not PI_API_KEY:
        return jsonify({"error": "Server haijawekewa ALFPAY_PI_API_KEY"}), 500

    body = request.get_json(force=True) or {}
    payment_id = body.get("paymentId")
    txid = body.get("txid")
    if not payment_id or not txid:
        return jsonify({"error": "Tuma 'paymentId' na 'txid'"}), 400

    resp = requests.post(
        f"{PI_PLATFORM_BASE}/payments/{payment_id}/complete",
        headers={"Authorization": f"Key {PI_API_KEY}"},
        json={"txid": txid},
    )
    if resp.status_code != 200:
        return jsonify({"error": "Imeshindwa kukamilisha malipo", "details": resp.text}), 400

    log_transaction({
        "type": "pi_complete",
        "payment_id": payment_id,
        "txid": txid,
        "timestamp": time.time(),
    })
    return jsonify(resp.json())


@app.route("/api/transactions", methods=["GET"])
def get_transactions():
    if TX_LOG_FILE.exists():
        return jsonify(json.loads(TX_LOG_FILE.read_text()))
    return jsonify([])


@app.route("/", methods=["GET"])
def health_check():
    return jsonify({"status": "ALFPayment backend inafanya kazi", "asset": ASSET_CODE})
