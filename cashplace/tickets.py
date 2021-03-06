from bit import Key, PrivateKeyTestnet, network
from errors import Unauthorized, TicketNotFound
from storage import data
from enum import Enum
import uuid
import time
import argon2
import logging

logger = logging.getLogger(__name__)


def clean(tickets_manager, config):
    # we must not change the size of dictionnary in  so let's use a queue
    to_delete = []
    for ticket_id in tickets_manager.tickets:
        ticket = tickets_manager.tickets[ticket_id]
        delay = time.time() - ticket.last_update

        if ticket.status == TicketStatus.CONFIGURATION:
            if delay > config.configuration_delay:
                to_delete.append(ticket_id)

        elif ticket.status == TicketStatus.RECEPTION:
            if delay > config.reception_delay:
                ticket.cancel()
                to_delete.append(ticket_id)

        elif ticket.status == TicketStatus.RECEIVED:
            if delay > config.received_delay:
                ticket.set_status(TicketStatus.SENT)

        elif ticket.status == TicketStatus.SENDING:
            # todo: check if bitcoins have been sent
            ticket.set_status(TicketStatus.DISPUTE)
            # else if the transaction didn't get confirmed within the
            # configured delay
            if delay > config.sending_delay:
                ticket.set_status(TicketStatus.RECEIVED)

        elif ticket.status == TicketStatus.SENT:
            if delay > config.sent_delay:
                to_delete.append(ticket_id)

        elif ticket.status == TicketStatus.DISPUTE:
            if delay > config.dispute_delay:
                ticket.cancel()
                to_delete.append(ticket_id)

    for ticket_id in to_delete:
        logger.warning(f"deleting ticket {ticket_id}")
        tickets_manager.delete_ticket(ticket_id)


class TicketStatus(Enum):
    CONFIGURATION = 0
    RECEPTION = 1
    RECEIVED = 2
    SENDING = 3
    SENT = 4
    DISPUTE = 5


class TicketsManager:
    def __init__(self, config):
        self.config = config
        BitcoinTicket.rate = config.btc_rate
        BitcoinTicket.master_address = config.btc_master_address
        BitcoinTicket.confirmations = config.btc_confirmations
        BitcoinTicket.static_minimal = config.btc_static_minimal
        BitcoinTicket.relative_minimal = config.btc_relative_minimal
        self.tickets = {}

    def load(self):
        tickets = data.load_all_tickets()
        for ticket_name in tickets:
            ticket_content = tickets[ticket_name]
            ticket = self.load_ticket(ticket_content)
            if ticket:
                self.tickets[ticket.id] = ticket

    def save(self):
        for ticket_id in self.tickets:
            self.tickets[ticket_id].save()

    def create_ticket(self, coin):
        if coin == "btc":
            ticket = BitcoinTicket.create(self.config.btc_testnet)
        else:
            return False
        self.tickets[ticket.id] = ticket
        ticket.save()
        return ticket

    def load_ticket(self, json_content):
        coin = json_content["coin"]
        if coin == "btc":
            return BitcoinTicket.load(json_content, self.config.btc_testnet)
        else:
            return False

    def delete_ticket(self, ticket_id):
        if not ticket_id in self.tickets:
            raise TicketNotFound(f"failed to delete ticket {ticket_id}")
        self.tickets.pop(ticket_id).delete()


class Ticket:
    def __init__(
        self,
        amount,
        spender_hash,
        spender_code,
        receiver_hash,
        receiver_code,
        master_is_spender,
        leftover_address,
        receiver_address,
        status,
    ):
        self.amount = amount
        self.spender_hash = spender_hash
        self.spender_code = spender_code if spender_code else str(uuid.uuid4())
        self.receiver_hash = receiver_hash
        self.receiver_code = receiver_code if receiver_code else str(uuid.uuid4())
        self.master_is_spender = master_is_spender
        self.leftover_address = leftover_address
        self.receiver_address = receiver_address
        self.status = status
        self.password_hasher = argon2.PasswordHasher()

    def save(self):
        data.save_ticket(self)

    def delete(self):
        data.delete_ticket(self.id)

    def update(self):
        self.last_update = time.time()
        self.save()

    def set_amount(self, amount, update=True):
        self.amount = amount
        if update:
            self.update()

    def set_leftover_address(self, address, update=True):
        self.leftover_address = address
        if update:
            self.update()

    def set_receiver_address(self, address, update=True):
        self.receiver_address = address
        if update:
            self.update()

    def set_status(self, status, update=True):
        self.status = status
        if update:
            self.update()

    def verify_password(self, password, spender):
        if password is None:
            raise Unauthorized("A password is required")

        if spender:
            if self.spender_hash is None:
                self.spender_hash = self.password_hasher.hash(password)
                if self.receiver_hash is None:
                    self.master_is_spender = True
                return
            password_hash = self.spender_hash

        else:
            if self.receiver_hash is None:
                self.receiver_hash = self.password_hasher.hash(password)
                if self.spender_hash is None:
                    self.master_is_spender = False
                return
            password_hash = self.receiver_hash
        try:
            self.password_hasher.verify(password_hash, password)
            if self.password_hasher.check_needs_rehash(password_hash):
                password_hash = self.password_hasher.hash(password)
            if spender:
                self.spender_hash = password_hash
            else:
                self.receiver_hash = password_hash
            self.update()
        except argon2.exceptions.VerifyMismatchError:
            raise Unauthorized("Wrong password")


class BitcoinTicket(Ticket):

    coin = "btc"

    @classmethod
    def create(cls, test):
        self = BitcoinTicket(PrivateKeyTestnet() if test else Key())
        self.test = test
        self.last_update = time.time()
        return self

    @classmethod
    def load(cls, json_content, test):
        wif = json_content["wif"]
        self = BitcoinTicket(
            PrivateKeyTestnet(wif) if test else Key(wif),
            json_content["amount"],
            json_content["spender_hash"],
            json_content["spender_code"],
            json_content["receiver_hash"],
            json_content["receiver_code"],
            json_content["master_is_spender"],
            json_content["leftover_address"],
            json_content["receiver_address"],
            TicketStatus(json_content["status"]),
        )
        self.test = test
        self.last_update = json_content["last_update"]
        return self

    def __init__(
        self,
        key,
        amount=0,
        spender_hash=None,
        spender_code=None,
        receiver_hash=None,
        receiver_code=None,
        master_is_spender=None,
        leftover_address=None,
        receiver_address=None,
        status=TicketStatus.CONFIGURATION,
    ):
        super().__init__(
            amount,
            spender_hash,
            spender_code,
            receiver_hash,
            receiver_code,
            master_is_spender,
            leftover_address,
            receiver_address,
            status,
        )
        self.key = key

    def fetch_balance(self):
        balance = 0
        for unspent in self.key.get_unspents():
            # temp fix for https://github.com/ofek/bit/issues/127
            confirmations = (
                unspent.confirmations + 1805352 if self.test else unspent.confirmations
            )
            if confirmations >= BitcoinTicket.confirmations:
                balance += unspent.amount
        return balance

    def refresh_balance(self):
        self.balance = self.fetch_balance()
        if self.balance >= self.amount:
            if self.status == TicketStatus.RECEPTION:
                self.set_status(TicketStatus.RECEIVED, False)
        elif self.balance == 0:
            if self.status == TicketStatus.RECEIVED:
                self.set_status(TicketStatus.SENDING, False)
        self.update()

    def cancel(self):
        self.key.create_transaction([], leftover=self.leftover_address)

    def finalize(self, fast=False):
        self.refresh_balance()
        fee = self.fetch_fee(fast)
        maximum_fee = (181 + 3 * 34 + 10) * fee
        cashplace_fee = int(self.amount * (1 - self.rate))
        if not cashplace_fee:
            cashplace_fee = 1
        transfer_amount = int(self.amount * self.rate)
        if not transfer_amount:
            transfer_amount = 1
        if self.balance - maximum_fee > self.amount:
            self.key.send(
                [
                    (self.master_address, cashplace_fee, "satoshi",),
                    (self.receiver_address, transfer_amount, "satoshi",),
                ],
                leftover=self.leftover_address,
                fee=fee,
            )

        else:
            self.key.send(
                [(self.master_address, transfer_amount, "satoshi",)],
                leftover=self.receiver_address,
                fee=fee,
            )

        self.set_status(TicketStatus.SENDING)

    def fetch_fee(self, fast):
        return 1 if self.test else network.get_fee(fast)

    def fetch_minimal_amount(self):
        return self.static_minimal + self.relative_minimal * self.fetch_fee(False)

    @property
    def id(self):
        return self.key.segwit_address

    @property
    def wif(self):
        return self.key.to_wif()

    @property
    def export(self):
        return {
            "coin": self.coin,
            "amount": self.amount,
            "wif": self.wif,
            "spender_hash": self.spender_hash,
            "spender_code": self.spender_code,
            "receiver_hash": self.receiver_hash,
            "receiver_code": self.receiver_code,
            "master_is_spender": self.master_is_spender,
            "leftover_address": self.leftover_address,
            "receiver_address": self.receiver_address,
            "status": self.status.value,
            "last_update": self.last_update,
        }
