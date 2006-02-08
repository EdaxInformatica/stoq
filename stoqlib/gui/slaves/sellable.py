# -*- Mode: Python; coding: iso-8859-1 -*-
# vi:si:et:sw=4:sts=4:ts=4

##
## Copyright (C) 2005, 2006 Async Open Source <http://www.async.com.br>
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
## Foundation, Inc., 59 Temple Place - Suite 330, Boston, MA 02111-1307,
## USA.
##
## Author(s):   Evandro Vale Miquelito      <evandro@async.com.br>
##
""" Slaves for sellables """

from stoqlib.gui.base.editors import BaseEditorSlave
from stoqlib.domain.sellable import OnSaleInfo
from stoqlib.lib.validators import get_price_format_str


class OnSaleInfoSlave(BaseEditorSlave):
    """A slave for price and dates information when a certain product,
    service or gift certificate is on sale.
    """
    gladefile = 'OnSaleInfoSlave'
    model_type = OnSaleInfo
    proxy_widgets = ('on_sale_price',
                     'on_sale_start_date',
                     'on_sale_end_date')

    #
    # BaseEditorSlave hooks
    #

    def create_model(self, conn):
        return OnSaleInfo(connection=conn)

    def setup_proxies(self):
        self.on_sale_price.set_data_format(get_price_format_str())
        self.proxy = self.add_proxy(self.model, self.proxy_widgets)
