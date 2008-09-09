# -*- coding: utf-8 -*-
# vi:si:et:sw=4:sts=4:ts=4

##
## Copyright (C) 2005-2007 Async Open Source <http://www.async.com.br>
## All rights reserved
##
## This program is free software; you can redistribute it and/or modify
## it under the terms of the GNU Lesser General Public License as published by
## the Free Software Foundation; either version 2 of the License, or
## (at your option) any later version.
##
## This program is distributed in the hope that it will be useful,
## but WITHOUT ANY WARRANTY; without even the implied warranty of
## MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
## GNU Lesser General Public License for more details.
##
## You should have received a copy of the GNU Lesser General Public License
## along with this program; if not, write to the Free Software
## Foundation, Inc., or visit: http://www.gnu.org/.
##
##  Author(s):  Evandro Vale Miquelito      <evandro@async.com.br>
##              Johan Dahlin                <jdahlin@async.com.br>
##
""" Purchase management """

from decimal import Decimal
import datetime

from kiwi.argcheck import argcheck
from kiwi.datatypes import currency
from zope.interface import implements
from stoqdrivers.enum import PaymentMethodType
from sqlobject import (ForeignKey, IntCol, DateTimeCol, UnicodeCol,
                       SQLObject)
from sqlobject.sqlbuilder import AND, INNERJOINOn, LEFTJOINOn, const
from sqlobject.viewable import Viewable

from stoqlib.database.columns import PriceCol, DecimalCol
from stoqlib.exceptions import DatabaseInconsistency, StoqlibError
from stoqlib.domain.base import ValidatableDomain, Domain, BaseSQLView
from stoqlib.domain.payment.methods import APaymentMethod
from stoqlib.domain.payment.group import AbstractPaymentGroup
from stoqlib.domain.interfaces import IPaymentGroup, IContainer, IDescribable
from stoqlib.domain.sellable import ASellable, BaseSellableInfo, SellableUnit
from stoqlib.lib.defaults import calculate_interval, quantize
from stoqlib.lib.translation import stoqlib_gettext
from stoqlib.lib.validators import format_quantity


_ = stoqlib_gettext


class PurchaseItem(Domain):
    """This class stores information of the purchased items.

    B{Importante attributes}
        - I{base_cost}: the cost which helps the purchaser to define the
                        main cost of a certain product.
    """
    quantity = DecimalCol(default=1)
    quantity_received = DecimalCol(default=0)
    base_cost = PriceCol()
    cost = PriceCol()
    sellable = ForeignKey('ASellable')
    order = ForeignKey('PurchaseOrder')

    def _create(self, id, **kw):
        if not 'sellable' in kw:
            raise TypeError('You must provide a sellable argument')
        if not 'order' in kw:
            raise TypeError('You must provide a order argument')

        # FIXME: Avoding shadowing sellable.cost
        kw['base_cost'] = kw['sellable'].cost

        if not 'cost' in kw:
            kw['cost'] = kw['sellable'].cost

        Domain._create(self, id, **kw)

    #
    # Accessors
    #

    def get_total(self):
        return currency(self.quantity * self.cost)

    def get_received_total(self):
        return currency(self.quantity_received * self.cost)

    def has_been_received(self):
        return self.quantity_received >= self.quantity

    def has_partial_received(self):
        return self.quantity_received > 0

    def get_pending_quantity(self):
        if not self.has_been_received:
            return Decimal(0)
        return self.quantity - self.quantity_received

    def get_quantity_as_string(self):
        unit = self.sellable.unit
        return "%s %s" % (format_quantity(self.quantity),
                          unit and unit.description or u"")

    def get_quantity_received_as_string(self):
        unit = self.sellable.unit
        return "%s %s" % (format_quantity(self.quantity_received),
                          unit and unit.description or u"")


class PurchaseOrder(ValidatableDomain):
    """Purchase and order definition."""

    implements(IContainer)

    (ORDER_CANCELLED,
     ORDER_QUOTING,
     ORDER_PENDING,
     ORDER_CONFIRMED,
     ORDER_CLOSED) = range(5)

    statuses = {ORDER_CANCELLED:    _(u'Cancelled'),
                ORDER_QUOTING:      _(u'Quoting'),
                ORDER_PENDING:      _(u'Pending'),
                ORDER_CONFIRMED:    _(u'Confirmed'),
                ORDER_CLOSED:       _(u'Closed')}

    (FREIGHT_FOB,
     FREIGHT_CIF) = range(2)

    freight_types = {FREIGHT_FOB    : _(u'FOB'),
                     FREIGHT_CIF    : _(u'CIF')}

    status = IntCol(default=ORDER_QUOTING)
    open_date = DateTimeCol(default=datetime.datetime.now)
    quote_deadline = DateTimeCol(default=None)
    expected_receival_date = DateTimeCol(default=datetime.datetime.now)
    expected_pay_date = DateTimeCol(default=datetime.datetime.now)
    receival_date = DateTimeCol(default=None)
    confirm_date = DateTimeCol(default=None)
    notes = UnicodeCol(default='')
    salesperson_name = UnicodeCol(default='')
    freight_type = IntCol(default=FREIGHT_FOB)
    freight = DecimalCol(default=0)
    surcharge_value = PriceCol(default=0)
    discount_value = PriceCol(default=0)
    supplier = ForeignKey('PersonAdaptToSupplier')
    branch = ForeignKey('PersonAdaptToBranch')
    transporter = ForeignKey('PersonAdaptToTransporter', default=None)

    #
    # IContainer Implementation
    #

    def get_items(self):
        return PurchaseItem.selectBy(order=self,
                                     connection=self.get_connection())

    @argcheck(PurchaseItem)
    def remove_item(self, item):
        conn = self.get_connection()
        if item.order is not self:
            raise ValueError('Argument item must have an order attribute '
                             'associated with the current purchase instance')
        PurchaseItem.delete(item.id, connection=conn)

    def add_item(self, sellable, quantity=Decimal(1)):
        conn = self.get_connection()
        return PurchaseItem(connection=conn, order=self,
                            sellable=sellable, quantity=quantity)

    #
    # Properties
    #

    def _set_discount_by_percentage(self, value):
        """Sets a discount by percentage.
        Note that percentage must be added as an absolute value not as a
        factor like 1.05 = 5 % of surcharge
        The correct form is 'percentage = 3' for a discount of 3 %"""
        self.discount_value = self._get_percentage_value(value)

    def _get_discount_by_percentage(self):
        discount_value = self.discount_value
        if not discount_value:
            return currency(0)
        subtotal = self.get_purchase_subtotal()
        assert subtotal > 0, ('the subtotal should not be zero '
                              'at this point')
        total = subtotal - discount_value
        percentage = (1 - total / subtotal) * 100
        return quantize(percentage)

    discount_percentage = property(_get_discount_by_percentage,
                                   _set_discount_by_percentage)

    def _set_surcharge_by_percentage(self, value):
        """Sets a surcharge by percentage.
        Note that surcharge must be added as an absolute value not as a
        factor like 0.97 = 3 % of discount.
        The correct form is 'percentage = 3' for a surcharge of 3 %"""
        self.surcharge_value = self._get_percentage_value(value)

    def _get_surcharge_by_percentage(self):
        surcharge_value = self.surcharge_value
        if not surcharge_value:
            return currency(0)
        subtotal = self.get_purchase_subtotal()
        assert subtotal > 0, ('the subtotal should not be zero '
                              'at this point')
        total = subtotal + surcharge_value
        percentage = ((total / subtotal) - 1) * 100
        return quantize(percentage)

    surcharge_percentage = property(_get_surcharge_by_percentage,
                                    _set_surcharge_by_percentage)

    @property
    def order_number(self):
        return self.id


    #
    # Private
    #

    def _get_percentage_value(self, percentage):
        if not percentage:
            return currency(0)
        subtotal = self.get_purchase_subtotal()
        percentage = Decimal(percentage)
        return subtotal * (percentage / 100)

    #
    # Public API
    #

    def create_preview_outpayments(self, conn, group, total):
        method = APaymentMethod.get_by_enum(
            conn, PaymentMethodType.get(group.default_method))
        if group.interval_type:
            interval = calculate_interval(group.interval_type, group.intervals)
            due_dates = []
            for i in range(group.installments_number):
                due_dates.append(self.expected_pay_date +
                                 datetime.timedelta(i * interval))

            method.create_outpayments(group, total, due_dates)
        else:
            method.create_outpayment(group, total)

    def confirm(self, confirm_date=None):
        """Confirms the purchase order

        @param confirm_data: optional, datetime
        """
        if confirm_date is None:
            confirm_date = const.NOW()

        if self.status != self.ORDER_PENDING:
            raise ValueError('Invalid order status, it should be '
                             'ORDER_PENDING, got %s'
                             % self.get_status_str())
        group = IPaymentGroup(self, None)
        if not group:
            raise ValueError('You must have a IPaymentGroup facet '
                             'defined at this point')
        group.confirm()
        self.status = self.ORDER_CONFIRMED
        self.confirm_date = confirm_date

    def close(self):
        """Closes the purchase order
        """
        if not self.status == self.ORDER_CONFIRMED:
            raise ValueError('Invalid status, it should be confirmed '
                             'got %s instead' % self.get_status_str())
        self.status = self.ORDER_CLOSED

    def cancel(self):
        """Cancels the purchase order
        """
        assert self.can_cancel()

        # we have to cancel the payments too
        group = IPaymentGroup(self, None)
        if group:
            for item in group.get_items():
                item.cancel()

        self.status = self.ORDER_CANCELLED

    def receive_item(self, item, quantity_to_receive):
        if not item in self.get_pending_items():
            raise StoqlibError('This item is not pending, hence '
                               'cannot be received')
        quantity = item.quantity - item.quantity_received
        if quantity < quantity_to_receive:
            raise StoqlibError('The quantity that you want to receive '
                               'is greater than the total quantity of '
                               'this item %r' % item)
        self.increase_quantity_received(item.sellable,
                                        quantity_to_receive)

    def can_cancel(self):
        """Find out if it's possible to cancel the order
        @returns: True if it's possible to cancel the order, otherwise False
        """
        # FIXME: Canceling partial orders disabled until we fix bug 3282
        for item in self.get_items():
            if item.has_partial_received():
                return False
        return self.status in [self.ORDER_QUOTING,
                               self.ORDER_PENDING,
                               self.ORDER_CONFIRMED]

    def can_close(self):
        """Find out if it's possible to close the order
        @returns: True if it's possible to close the order, otherwise False
        """
        for item in self.get_items():
            if not item.has_been_received():
                return False
        return True

    def increase_quantity_received(self, sellable, quantity_received):
        items = [item for item in self.get_items()
                    if item.sellable.id == sellable.id]
        qty = len(items)
        if not qty:
            raise ValueError('There is no purchase item for '
                             'sellable %r' % sellable)
        if qty > 1:
            raise DatabaseInconsistency('It should have only one item for '
                                        'this sellable, got %d instead'
                                        % qty)
        item = items[0]
        item.quantity_received += quantity_received

    def get_status_str(self):
        return PurchaseOrder.translate_status(self.status)

    def get_freight_type_name(self):
        if not self.freight_type in self.freight_types.keys():
            raise DatabaseInconsistency('Invalid freight_type, got %d'
                                        % self.freight_type)
        return self.freight_types[self.freight_type]

    def get_freight(self):
        """
        Gets the percentage value of freight in agreement of freight's type.
        @returns: the percentage value of freight.
        """
        type_freight = self.get_freight_type_name()
        if type_freight == "FOB":
            return self.freight
        elif type_freight == "CIF" and self.transporter:
            return self.transporter.freight_percentage or currency(0)
        else:
            return currency(0)

    def get_branch_name(self):
        return self.branch.get_description()

    def get_supplier_name(self):
        return self.supplier.get_description()

    def get_transporter_name(self):
        if not self.transporter:
            return u""
        return self.transporter.get_description()

    def get_order_number_str(self):
        return u'%05d' % self.id

    def get_purchase_subtotal(self):
        """Get the subtotal of the purchase.
        The sum of all the items cost * items quantity
        """
        return currency(self.get_items().sum(
            PurchaseItem.q.cost *PurchaseItem.q.quantity) or 0)

    def get_purchase_total(self):
        subtotal = self.get_purchase_subtotal()
        total = subtotal - self.discount_value + self.surcharge_value
        if total < 0:
            raise ValueError('Purchase total can not be lesser than zero')
        return currency(total)

    def get_received_total(self):
        """Like {get_purchase_subtotal} but only takes into account the
        received items
        """
        return currency(self.get_items().sum(
            PurchaseItem.q.cost *
            PurchaseItem.q.quantity_received) or 0)

    def get_remaining_total(self):
        """The total value to be paid for the items not received yet
        """
        return self.get_purchase_total() - self.get_received_total()

    def get_pending_items(self):
        """
        Returns a sequence of all items which we haven't received yet.
        """
        return self.get_items().filter(
            PurchaseItem.q.quantity_received < PurchaseItem.q.quantity)

    def get_partially_received_items(self):
        """
        Returns a sequence of all items which are partially received.
        """
        return self.get_items().filter(
            PurchaseItem.q.quantity_received > 0)

    def get_open_date_as_string(self):
        return self.open_date and self.open_date.strftime("%x") or ""

    def get_quote_deadline_as_string(self):
        return self.quote_deadline and self.quote_deadline.strftime("%x") or ""

    def get_receiving_orders(self):
        """Returns all ReceivingOrder related to this purchase order
        """
        from stoqlib.domain.receiving import ReceivingOrder
        return ReceivingOrder.selectBy(purchase=self,
                                       connection=self.get_connection())

    #
    # Classmethods
    #

    @classmethod
    def translate_status(cls, status):
        if not cls.statuses.has_key(status):
            raise DatabaseInconsistency('Got an unexpected status value: '
                                        '%s' % status)
        return cls.statuses[status]


class Quotation(Domain):
    group = ForeignKey('QuoteGroup')
    purchase = ForeignKey('PurchaseOrder')

    implements(IDescribable)

    def get_description(self):
        supplier = self.purchase.supplier.person.name
        return "Group %04d - %s" % (self.group.id, supplier)

    #
    # Public API
    #

    def close(self):
        """Closes the quotation"""
        # we don't have a specific status for closed quotes, so we keep the
        # current status (quoting) and set it as an invalid model
        if not self.is_closed():
            self.purchase.set_invalid()

    def is_closed(self):
        """Returns if the quotation is closed or not.
        @returns: True if the quotation is closed, False otherwise.
        """
        return not self.purchase.get_valid()


class QuoteGroup(Domain):

    implements(IContainer, IDescribable)

    #
    # IContainer
    #

    def get_items(self):
        return Quotation.selectBy(group=self, connection=self.get_connection())

    @argcheck(Quotation)
    def remove_item(self, item):
        conn = self.get_connection()
        if item.group is not self:
            raise ValueError('You can not remove an item which does not '
                             'belong to this group.')

        order = item.purchase
        Quotation.delete(item.id, connection=conn)
        for order_item in order.get_items():
            order.remove_item(order_item)
        PurchaseOrder.delete(order.id, connection=conn)

    @argcheck(PurchaseOrder)
    def add_item(self, item):
        conn = self.get_connection()
        return Quotation(purchase=item, group=self, connection=conn)

    #
    # IDescribable
    #

    def get_description(self):
        return _(u"quote number %04d" % self.id)

    #
    # Public API
    #

    def cancel(self):
        """Cancel a quote group."""
        conn = self.get_connection()
        for quote in self.get_items():
            quote.purchase.cancel()
            Quotation.delete(quote.id, connection=conn)


class PurchaseOrderAdaptToPaymentGroup(AbstractPaymentGroup):

    _inheritable = False

    #
    # IPaymentGroup implementation
    #

#     def set_thirdparty(self, person):
#         """Define a new thirdparty. The parameter is a person, but also have
#         to implement specific facets to each PaymentGroup adapter. """
#         supplier = ISupplier(person)
#         if not supplier:
#             raise StoqlibError("the purchase thirdparty should have an "
#                                "ISupplier facet at this point")
#         order = self.get_adapted()
#         order.supplier = supplier

    def confirm(self):
        for payment in self.get_items():
            payment.set_pending()

    def pay(self, payment):
        pass

    def get_thirdparty(self):
        order = self.get_adapted()
        if not order.supplier:
            raise DatabaseInconsistency('An order must have a supplier')
        return order.supplier.person

    def get_group_description(self):
        order = self.get_adapted()
        return _(u'order %s') % order.id

PurchaseOrder.registerFacet(PurchaseOrderAdaptToPaymentGroup, IPaymentGroup)


class PurchaseItemView(Viewable):
    """This is a view which you can use to fetch purchase items within
    a specific purchase. It's used by the PurchaseDetails dialog
    to display all the purchase items within a purchase

    @param id: id of the purchase item
    @param purchase_id: id of the purchase order the item belongs to
    @param sellable: sellable of the item
    @param cost: cost of the item
    @param quantity: quantity ordered
    @param quantity_received: quantity received
    @param total: total value of the items purchased
    @param total_received: total value of the items received
    @param description: description of the sellable
    @param unit: unit as a string or None if the product has no unit
    """
    columns = dict(
        id=PurchaseItem.q.id,
        purchase_id=PurchaseOrder.q.id,
        sellable=ASellable.q.id,
        cost=PurchaseItem.q.cost,
        quantity=PurchaseItem.q.quantity,
        quantity_received=PurchaseItem.q.quantity_received,
        total=PurchaseItem.q.cost * PurchaseItem.q.quantity,
        total_received=PurchaseItem.q.cost * PurchaseItem.q.quantity_received,
        description=BaseSellableInfo.q.description,
        unit=SellableUnit.q.description,
        )

    clause = AND(
        PurchaseOrder.q.id == PurchaseItem.q.orderID,
        BaseSellableInfo.q.id == ASellable.q.base_sellable_infoID,
        )

    joins = [
        INNERJOINOn(None, ASellable,
                    ASellable.q.id == PurchaseItem.q.sellableID),
        LEFTJOINOn(None, SellableUnit,
                   SellableUnit.q.id == ASellable.q.unitID),
        ]

    def get_quantity_as_string(self):
        return "%s %s" % (format_quantity(self.quantity),
                          self.unit or u"")


    def get_quantity_received_as_string(self):
        return "%s %s" % (format_quantity(self.quantity_received),
                          self.unit or u"")

    @classmethod
    def select_by_purchase(cls, purchase, connection):
        return PurchaseItemView.select(PurchaseOrder.q.id == purchase.id,
                                       connection=connection)


#
# Views
#

class PurchaseOrderView(SQLObject, BaseSQLView):
    """General information about purchase orders"""
    status = IntCol()
    open_date = DateTimeCol()
    quote_deadline = DateTimeCol()
    expected_receival_date = DateTimeCol()
    expected_pay_date = DateTimeCol()
    receival_date = DateTimeCol()
    confirm_date = DateTimeCol()
    salesperson_name = UnicodeCol()
    freight = DecimalCol()
    surcharge_value = PriceCol()
    discount_value = PriceCol()
    supplier_name = UnicodeCol()
    transporter_name = UnicodeCol()
    branch_name = UnicodeCol()
    ordered_quantity = DecimalCol()
    received_quantity = DecimalCol()
    subtotal = DecimalCol()
    total = DecimalCol()

    def get_transporter_name(self):
        return self.transporter_name or u""

    def get_open_date_as_string(self):
        return self.open_date.strftime("%x")

    def get_status_str(self):
        return PurchaseOrder.translate_status(self.status)

    @property
    def purchase(self):
        return PurchaseOrder.get(self.id)

    def get_status_name(self):
        return self.purchase.translate_status(self.status)
