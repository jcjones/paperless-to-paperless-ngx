from pathlib import Path
from time import sleep
from datetime import datetime, timezone, timedelta
import argparse
import json
import logging

import requests
import progressbar
from peewee import *

DB = SqliteDatabase(None)
OUT_PATH = "doc_output"


PARSER = argparse.ArgumentParser(description=__doc__)

PARSER.add_argument(
    "--log-level",
    default=logging.INFO,
    type=lambda x: getattr(logging, x.upper()),
    help="Configure the logging level.",
)
PARSER.add_argument("library", type=Path, help="The path to 'Receipt Library.paperless'")
PARSER.add_argument("--auth", help="Auth token", required=True)
PARSER.add_argument("--noop", action="store_true")
PARSER.add_argument("--url", required=True)
PARSER.add_argument("--start", type=int, default=0)
PARSER.add_argument("--count", type=int, default=None)

class BaseModel(Model):
    class Meta:
        database = DB


class Category(BaseModel):
    pk = IntegerField(primary_key=True, column_name="Z_PK")
    name = CharField(column_name="ZNAME")

    class Meta:
        table_name = "ZCATEGORY"


class SubCategory(BaseModel):
    pk = IntegerField(primary_key=True, column_name="Z_PK")
    name = CharField(column_name="ZNAME")

    class Meta:
        table_name = "ZSUBCATEGORY"


class Receipt(BaseModel):
    pk = IntegerField(primary_key=True, column_name="Z_PK")
    merchant = CharField(column_name="ZMERCHANT")
    notes = TextField(column_name="ZNOTES")
    path = CharField(column_name="ZPATH")
    amount = FloatField(column_name="ZAMOUNT")
    amount_s = CharField(column_name="ZAMOUNTASSTRING")
    timestamp = IntegerField(column_name="ZDATE")
    category = ForeignKeyField(Category, column_name="ZCATEGORY")
    subcategory = ForeignKeyField(SubCategory, column_name="ZSUBCATEGORY")

    class Meta:
        table_name = "ZRECEIPT"


class Tag(BaseModel):
    pk = IntegerField(primary_key=True, column_name="Z_PK")
    name = CharField(column_name="ZNAME")

    class Meta:
        table_name = "ZTAG"


class ReceiptTag(BaseModel):
    receipt = ForeignKeyField(
        Receipt, column_name="Z_14RECEIPTS1", backref="receipt_tags"
    )
    tag = ForeignKeyField(Tag, column_name="Z_18TAGS", backref="receipt_tags")

    class Meta:
        table_name = "Z_14TAGS"
        primary_key = False


def file_name(index, receipt):
    if receipt.merchant and receipt.merchant.strip():
        return f"{index}-{receipt.merchant.strip()}.pdf"
    try:
        return f"{index}-{receipt.category.name}.pdf"
    except Category.DoesNotExist:
        return f"{index}-no-name.pdf"

def collect_tags(receipt):
    tags = []
    try:
        tags.append(receipt.category.name.lower())
    except Category.DoesNotExist:
        pass
    try:
        tags.append(receipt.subcategory.name.lower())
    except SubCategory.DoesNotExist:
        pass

    for rtag in receipt.receipt_tags:
        tags.append(rtag.tag.name.strip().lower())

    return set(tags)

def wait_for_doc_publication(session, url, uuid):
    while True:
        result = session.get(url + f"/api/tasks/?task_id={uuid}")
        result.raise_for_status()
        task_results = result.json()[0]
        logging.debug("%s status is %s", uuid, task_results['status'])

        if task_results['status'] in ["PENDING", "STARTED"]:
            sleep(2)
            continue

        if task_results['status'] in ["SUCCESS"]:
            return task_results['related_document'], True

        if 'is a duplicate of' in task_results['result']:
            logging.warning("%s was a duplicate of DocID %s", task_results['task_file_name'], task_results['related_document'])
            return task_results['related_document'], False

        logging.error("Result: [%s] %s", task_results['status'], task_results)
        raise Exception("Unexpected publication status", task_results)
        # return None, False

class PaperlessField:
    def __init__(self, session, url):
        self._session = session
        self._url = url
        self._name_to_id = {}

        get_url = self._url
        while True:
            result = self._session.get(get_url)
            result.raise_for_status()
            data = result.json()
            for c in data['results']:
                self._name_to_id[c['name']] = c['id']
            if not data['next']:
                break
            get_url = data['next'].replace("http:", "https:")

    def get(self, name):
        if name not in self._name_to_id:
            self.put(name)
        return self._name_to_id[name]

    def find(self, name):
        for key, value in self._name_to_id.items():
            if key in name:
                return value
        return self.get(name)

    def put(self, name):
        data={'name': name}
        result = self._session.post(self._url, data=data)
        result.raise_for_status()
        self._name_to_id[name] = result.json()['id']

def find_date(receipt):
    last_element = receipt.path.rfind('/')
    date_str = receipt.path[:last_element]
    return datetime.strptime(f"{date_str} +0000", "Documents/%Y/%m/%d %z")

def run():
    # create_out_dir(OUT_PATH)
    args = PARSER.parse_args()
    log_format = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    logging.basicConfig(level=args.log_level, format=log_format)
    progressbar.streams.wrap_stderr()

    DB.init(args.library / "DocumentWallet.documentwalletsql")

    query = Receipt.select(Receipt)
    session = requests.Session()
    session.headers = { 'Authorization': f"Token {args.auth}" }

    correspondants=PaperlessField(session, args.url + "/api/correspondents/")
    tags=PaperlessField(session, args.url + "/api/tags/")

    for index, receipt in enumerate(progressbar.progressbar(query)):
        if index < args.start:
            continue
        if args.count and index >= args.start + args.count:
            break

        _file_name = file_name(index, receipt)

        files={'document': (Path(receipt.path).name, open(args.library / receipt.path, 'rb'))}
        data={
            'title': _file_name,
            'created': find_date(receipt),
        }

        logging.info("%d: Processing %s [%s] %f %s", index, args.library / receipt.path, data, receipt.amount, receipt.amount_s)
        if args.noop:
            continue

        tag_list = []
        for tag_name in collect_tags(receipt):
            tag_list.append(tags.get(tag_name))
        if tag_list:
            data['tags'] = tag_list

        if receipt.merchant:
            data['correspondent'] = correspondants.get(receipt.merchant)

        result = session.post(args.url + "/api/documents/post_document/", data=data, files=files)
        result.raise_for_status()
        uuid = result.text.strip('"')
        logging.info("Task submitted: %s", uuid)

        doc_id, is_new = wait_for_doc_publication(session, args.url, uuid)

        post_updates = {}
        if receipt.amount > 0:
            post_updates['custom_fields'] = [{'field': 1, 'value': f"USD{receipt.amount}" }]
        if receipt.notes:
            post_updates['notes'] = receipt.notes.strip()

        if post_updates:
            if not doc_id:
                logging.error("Can't update notes and such for %d: [%s]", index, _file_name)
            else:
                result = session.patch(args.url + f"/api/documents/{doc_id}/", json=post_updates)
                result.raise_for_status()

        logging.info("done with IDX %d: [%s] DocID: %s", index, _file_name, doc_id)
        logging.info("Use --start from %d", index+1)

if __name__ == "__main__":
    run()
