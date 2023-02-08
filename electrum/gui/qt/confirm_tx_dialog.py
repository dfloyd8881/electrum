#!/usr/bin/env python
#
# Electrum - lightweight Bitcoin client
# Copyright (2019) The Electrum Developers
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation files
# (the "Software"), to deal in the Software without restriction,
# including without limitation the rights to use, copy, modify, merge,
# publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS
# BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN
# ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

from decimal import Decimal
from functools import partial
from typing import TYPE_CHECKING, Optional, Union

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QIcon

from PyQt5.QtWidgets import QWidget, QHBoxLayout, QVBoxLayout, QLabel, QGridLayout, QPushButton, QLineEdit, QToolButton, QMenu

from electrum.i18n import _
from electrum.util import NotEnoughFunds, NoDynamicFeeEstimates
from electrum.util import quantize_feerate
from electrum.plugin import run_hook
from electrum.transaction import Transaction, PartialTransaction
from electrum.wallet import InternalAddressCorruption
from electrum.simple_config import SimpleConfig

from .util import (WindowModalDialog, ColorScheme, HelpLabel, Buttons, CancelButton,
                   BlockingWaitingDialog, PasswordLineEdit, WWLabel, read_QIcon)

from .fee_slider import FeeSlider, FeeComboBox

if TYPE_CHECKING:
    from .main_window import ElectrumWindow

from .transaction_dialog import TxSizeLabel, TxFiatLabel, TxInOutWidget
from .fee_slider import FeeSlider, FeeComboBox
from .amountedit import FeerateEdit, BTCAmountEdit
from .locktimeedit import LockTimeEdit


class TxEditor(WindowModalDialog):

    def __init__(self, *, title='',
                 window: 'ElectrumWindow',
                 make_tx,
                 output_value: Union[int, str] = None,
                 allow_preview=True):

        WindowModalDialog.__init__(self, window, title=title)
        self.main_window = window
        self.make_tx = make_tx
        self.output_value = output_value
        self.tx = None  # type: Optional[PartialTransaction]
        self.config = window.config
        self.wallet = window.wallet
        self.not_enough_funds = False
        self.no_dynfee_estimates = False
        self.needs_update = False
        # preview is disabled for lightning channel funding
        self.allow_preview = allow_preview
        self.is_preview = False

        self.locktime_e = LockTimeEdit(self)
        self.locktime_label = QLabel(_("LockTime") + ": ")
        self.io_widget = TxInOutWidget(self.main_window, self.wallet)
        self.create_fee_controls()

        vbox = QVBoxLayout()
        self.setLayout(vbox)

        top = self.create_top_bar(self.help_text)
        grid = self.create_grid()

        vbox.addLayout(top)
        vbox.addLayout(grid)
        self.message_label = WWLabel('\n')
        vbox.addWidget(self.message_label)
        vbox.addWidget(self.io_widget)
        buttons = self.create_buttons_bar()
        vbox.addStretch(1)
        vbox.addLayout(buttons)

        self.set_io_visible(self.config.get('show_tx_io', False))
        self.set_fee_edit_visible(self.config.get('show_tx_fee_details', False))
        self.set_locktime_visible(self.config.get('show_tx_locktime', False))
        self.set_preview_visible(self.config.get('show_tx_preview_button', False))
        self.update_fee_target()
        self.resize(self.layout().sizeHint())

        self.main_window.gui_object.timer.timeout.connect(self.timer_actions)


    def timer_actions(self):
        if self.needs_update:
            self.update_tx()
            self.update()
            self.needs_update = False

    def stop_editor_updates(self):
        self.main_window.gui_object.timer.timeout.disconnect(self.timer_actions)

    def set_fee_config(self, dyn, pos, fee_rate):
        if dyn:
            if self.config.use_mempool_fees():
                self.config.set_key('depth_level', pos, False)
            else:
                self.config.set_key('fee_level', pos, False)
        else:
            self.config.set_key('fee_per_kb', fee_rate, False)

    def update_tx(self, *, fallback_to_zero_fee: bool = False):
        raise NotImplementedError()

    def update_fee_target(self):
        text = self.fee_slider.get_dynfee_target()
        self.fee_target.setText(text)
        self.fee_target.setVisible(bool(text)) # hide in static mode

    def update_feerate_label(self):
        self.feerate_label.setText(self.feerate_e.text() + ' ' + self.feerate_e.base_unit())

    def create_fee_controls(self):

        self.fee_label = QLabel('')
        self.fee_label.setTextInteractionFlags(Qt.TextSelectableByMouse)

        self.size_label = TxSizeLabel()
        self.size_label.setAlignment(Qt.AlignCenter)
        self.size_label.setAmount(0)
        self.size_label.setStyleSheet(ColorScheme.DEFAULT.as_stylesheet())

        self.feerate_label = QLabel('')
        self.feerate_label.setTextInteractionFlags(Qt.TextSelectableByMouse)

        self.fiat_fee_label = TxFiatLabel()
        self.fiat_fee_label.setAlignment(Qt.AlignCenter)
        self.fiat_fee_label.setAmount(0)
        self.fiat_fee_label.setStyleSheet(ColorScheme.DEFAULT.as_stylesheet())

        self.feerate_e = FeerateEdit(lambda: 0)
        self.feerate_e.setAmount(self.config.fee_per_byte())
        self.feerate_e.textEdited.connect(partial(self.on_fee_or_feerate, self.feerate_e, False))
        self.feerate_e.editingFinished.connect(partial(self.on_fee_or_feerate, self.feerate_e, True))
        self.update_feerate_label()

        self.fee_e = BTCAmountEdit(self.main_window.get_decimal_point)
        self.fee_e.textEdited.connect(partial(self.on_fee_or_feerate, self.fee_e, False))
        self.fee_e.editingFinished.connect(partial(self.on_fee_or_feerate, self.fee_e, True))

        self.feerate_e.setFixedWidth(150)
        self.fee_e.setFixedWidth(150)

        self.fee_e.textChanged.connect(self.entry_changed)
        self.feerate_e.textChanged.connect(self.entry_changed)

        self.fee_target = QLabel('')
        self.fee_slider = FeeSlider(self, self.config, self.fee_slider_callback)
        self.fee_combo = FeeComboBox(self.fee_slider)

        def feerounding_onclick():
            text = (self.feerounding_text + '\n\n' +
                    _('To somewhat protect your privacy, Electrum tries to create change with similar precision to other outputs.') + ' ' +
                    _('At most 100 satoshis might be lost due to this rounding.') + ' ' +
                    _("You can disable this setting in '{}'.").format(_('Preferences')) + '\n' +
                    _('Also, dust is not kept as change, but added to the fee.')  + '\n' +
                    _('Also, when batching RBF transactions, BIP 125 imposes a lower bound on the fee.'))
            self.show_message(title=_('Fee rounding'), msg=text)

        self.feerounding_icon = QToolButton()
        self.feerounding_icon.setStyleSheet("background-color: rgba(255, 255, 255, 0); ")
        self.feerounding_icon.setAutoRaise(True)
        self.feerounding_icon.clicked.connect(feerounding_onclick)
        self.set_feerounding_visibility(False)

        self.fee_hbox = fee_hbox = QHBoxLayout()
        fee_hbox.addWidget(self.feerate_e)
        fee_hbox.addWidget(self.feerate_label)
        fee_hbox.addWidget(self.size_label)
        fee_hbox.addWidget(self.fee_e)
        fee_hbox.addWidget(self.fee_label)
        fee_hbox.addWidget(self.fiat_fee_label)
        fee_hbox.addWidget(self.feerounding_icon)
        fee_hbox.addStretch()

        self.fee_target_hbox = fee_target_hbox = QHBoxLayout()
        fee_target_hbox.addWidget(self.fee_target)
        fee_target_hbox.addWidget(self.fee_slider)
        fee_target_hbox.addWidget(self.fee_combo)
        fee_target_hbox.addStretch()

        # set feerate_label to same size as feerate_e
        self.feerate_label.setFixedSize(self.feerate_e.sizeHint())
        self.fee_label.setFixedSize(self.fee_e.sizeHint())
        self.fee_slider.setFixedWidth(200)
        self.fee_target.setFixedSize(self.feerate_e.sizeHint())

    def _trigger_update(self):
        # set tx to None so that the ok button is disabled while we compute the new tx
        self.tx = None
        self.update()
        self.needs_update = True

    def fee_slider_callback(self, dyn, pos, fee_rate):
        self.set_fee_config(dyn, pos, fee_rate)
        self.fee_slider.activate()
        if fee_rate:
            fee_rate = Decimal(fee_rate)
            self.feerate_e.setAmount(quantize_feerate(fee_rate / 1000))
        else:
            self.feerate_e.setAmount(None)
        self.fee_e.setModified(False)
        self.update_fee_target()
        self.update_feerate_label()
        self._trigger_update()

    def on_fee_or_feerate(self, edit_changed, editing_finished):
        edit_other = self.feerate_e if edit_changed == self.fee_e else self.fee_e
        if editing_finished:
            if edit_changed.get_amount() is None:
                # This is so that when the user blanks the fee and moves on,
                # we go back to auto-calculate mode and put a fee back.
                edit_changed.setModified(False)
        else:
            # edit_changed was edited just now, so make sure we will
            # freeze the correct fee setting (this)
            edit_other.setModified(False)
        self.fee_slider.deactivate()
        self._trigger_update()

    def is_send_fee_frozen(self):
        return self.fee_e.isVisible() and self.fee_e.isModified() \
               and (self.fee_e.text() or self.fee_e.hasFocus())

    def is_send_feerate_frozen(self):
        return self.feerate_e.isVisible() and self.feerate_e.isModified() \
               and (self.feerate_e.text() or self.feerate_e.hasFocus())

    def set_feerounding_text(self, num_satoshis_added):
        self.feerounding_text = (_('Additional {} satoshis are going to be added.')
                                 .format(num_satoshis_added))

    def set_feerounding_visibility(self, b:bool):
        # we do not use setVisible because it affects the layout
        self.feerounding_icon.setIcon(read_QIcon('info.png') if b else QIcon())
        self.feerounding_icon.setEnabled(b)

    def get_fee_estimator(self):
        if self.is_send_fee_frozen() and self.fee_e.get_amount() is not None:
            fee_estimator = self.fee_e.get_amount()
        elif self.is_send_feerate_frozen() and self.feerate_e.get_amount() is not None:
            amount = self.feerate_e.get_amount()  # sat/byte feerate
            amount = 0 if amount is None else amount * 1000  # sat/kilobyte feerate
            fee_estimator = partial(
                SimpleConfig.estimate_fee_for_feerate, amount)
        else:
            fee_estimator = None
        return fee_estimator

    def entry_changed(self):
        # blue color denotes auto-filled values
        text = ""
        fee_color = ColorScheme.DEFAULT
        feerate_color = ColorScheme.DEFAULT
        if self.not_enough_funds:
            fee_color = ColorScheme.RED
            feerate_color = ColorScheme.RED
        elif self.fee_e.isModified():
            feerate_color = ColorScheme.BLUE
        elif self.feerate_e.isModified():
            fee_color = ColorScheme.BLUE
        else:
            fee_color = ColorScheme.BLUE
            feerate_color = ColorScheme.BLUE
        self.fee_e.setStyleSheet(fee_color.as_stylesheet())
        self.feerate_e.setStyleSheet(feerate_color.as_stylesheet())
        #
        self.needs_update = True

    def update_fee_fields(self):
        freeze_fee = self.is_send_fee_frozen()
        freeze_feerate = self.is_send_feerate_frozen()
        tx = self.tx
        if self.no_dynfee_estimates and tx:
            size = tx.estimated_size()
            self.size_label.setAmount(size)
            #self.size_e.setAmount(size)
        if self.not_enough_funds or self.no_dynfee_estimates:
            if not freeze_fee:
                self.fee_e.setAmount(None)
            if not freeze_feerate:
                self.feerate_e.setAmount(None)
            self.set_feerounding_visibility(False)
            return

        assert tx is not None
        size = tx.estimated_size()
        fee = tx.get_fee()

        #self.size_e.setAmount(size)
        self.size_label.setAmount(size)
        fiat_fee = self.main_window.format_fiat_and_units(fee)
        self.fiat_fee_label.setAmount(fiat_fee)

        # Displayed fee/fee_rate values are set according to user input.
        # Due to rounding or dropping dust in CoinChooser,
        # actual fees often differ somewhat.
        if freeze_feerate or self.fee_slider.is_active():
            displayed_feerate = self.feerate_e.get_amount()
            if displayed_feerate is not None:
                displayed_feerate = quantize_feerate(displayed_feerate)
            elif self.fee_slider.is_active():
                # fallback to actual fee
                displayed_feerate = quantize_feerate(fee / size) if fee is not None else None
                self.feerate_e.setAmount(displayed_feerate)
            displayed_fee = round(displayed_feerate * size) if displayed_feerate is not None else None
            self.fee_e.setAmount(displayed_fee)
        else:
            if freeze_fee:
                displayed_fee = self.fee_e.get_amount()
            else:
                # fallback to actual fee if nothing is frozen
                displayed_fee = fee
                self.fee_e.setAmount(displayed_fee)
            displayed_fee = displayed_fee if displayed_fee else 0
            displayed_feerate = quantize_feerate(displayed_fee / size) if displayed_fee is not None else None
            self.feerate_e.setAmount(displayed_feerate)

        # set fee rounding icon to empty if there is no rounding
        feerounding = (fee - displayed_fee) if (fee and displayed_fee is not None) else 0
        self.set_feerounding_text(int(feerounding))
        self.feerounding_icon.setToolTip(self.feerounding_text)
        self.set_feerounding_visibility(abs(feerounding) >= 1)

    def create_buttons_bar(self):
        self.preview_button = QPushButton(_('Preview'))
        self.preview_button.clicked.connect(self.on_preview)
        self.ok_button = QPushButton(_('OK'))
        self.ok_button.clicked.connect(self.on_send)
        self.ok_button.setDefault(True)
        buttons = Buttons(CancelButton(self), self.preview_button, self.ok_button)
        return buttons

    def create_top_bar(self, text):
        self.pref_menu = QMenu()
        self.m1 = self.pref_menu.addAction('Show inputs/outputs', self.toggle_io_visibility)
        self.m1.setCheckable(True)
        self.m2 = self.pref_menu.addAction('Edit fees', self.toggle_fee_details)
        self.m2.setCheckable(True)
        self.m3 = self.pref_menu.addAction('Edit Locktime', self.toggle_locktime)
        self.m3.setCheckable(True)
        self.m4 = self.pref_menu.addAction('Show Preview Button', self.toggle_preview_button)
        self.m4.setCheckable(True)
        self.m4.setEnabled(self.allow_preview)
        self.pref_button = QToolButton()
        self.pref_button.setIcon(read_QIcon("preferences.png"))
        self.pref_button.setMenu(self.pref_menu)
        self.pref_button.setPopupMode(QToolButton.InstantPopup)
        hbox = QHBoxLayout()
        hbox.addWidget(QLabel(text))
        hbox.addStretch()
        hbox.addWidget(self.pref_button)
        return hbox

    def toggle_io_visibility(self):
        b = not self.config.get('show_tx_io', False)
        self.config.set_key('show_tx_io', b)
        self.set_io_visible(b)
        #self.resize(self.layout().sizeHint())
        self.setFixedSize(self.layout().sizeHint())

    def toggle_fee_details(self):
        b = not self.config.get('show_tx_fee_details', False)
        self.config.set_key('show_tx_fee_details', b)
        self.set_fee_edit_visible(b)
        self.setFixedSize(self.layout().sizeHint())

    def toggle_locktime(self):
        b = not self.config.get('show_tx_locktime', False)
        self.config.set_key('show_tx_locktime', b)
        self.set_locktime_visible(b)
        self.setFixedSize(self.layout().sizeHint())

    def toggle_preview_button(self):
        b = not self.config.get('show_tx_preview_button', False)
        self.config.set_key('show_tx_preview_button', b)
        self.set_preview_visible(b)

    def set_preview_visible(self, b):
        b = b and self.allow_preview
        self.preview_button.setVisible(b)
        self.m4.setChecked(b)

    def set_io_visible(self, b):
        self.io_widget.setVisible(b)
        self.m1.setChecked(b)

    def set_fee_edit_visible(self, b):
        detailed = [self.feerounding_icon, self.feerate_e, self.fee_e]
        basic = [self.fee_label, self.feerate_label]
        # first hide, then show
        for w in (basic if b else detailed):
            w.hide()
        for w in (detailed if b else basic):
            w.show()
        self.m2.setChecked(b)

    def set_locktime_visible(self, b):
        for w in [
                self.locktime_e,
                self.locktime_label]:
            w.setVisible(b)
        self.m3.setChecked(b)

    def run(self):
        cancelled = not self.exec_()
        self.stop_editor_updates()
        self.deleteLater()  # see #3956
        return self.tx if not cancelled else None

    def on_send(self):
        self.accept()

    def on_preview(self):
        self.is_preview = True
        self.accept()

    def toggle_send_button(self, enable: bool, *, message: str = None):
        if message is None:
            self.message_label.setStyleSheet(None)
            self.message_label.setText(' ')
        else:
            self.message_label.setStyleSheet(ColorScheme.RED.as_stylesheet())
            self.message_label.setText(message)

        self.setFixedSize(self.layout().sizeHint())
        self.preview_button.setEnabled(enable)
        self.ok_button.setEnabled(enable)

    def update(self):
        tx = self.tx
        self._update_amount_label()
        if self.not_enough_funds:
            text = self.main_window.send_tab.get_text_not_enough_funds_mentioning_frozen()
            self.toggle_send_button(False, message=text)
            return
        if not tx:
            self.toggle_send_button(False)
            self.set_feerounding_visibility(False)
            return
        self.update_fee_fields()
        if self.locktime_e.get_locktime() is None:
            self.locktime_e.set_locktime(self.tx.locktime)
        self.io_widget.update(tx)
        fee = tx.get_fee()
        assert fee is not None
        self.fee_label.setText(self.main_window.config.format_amount_and_units(fee))

        fee_rate = fee // tx.estimated_size()
        #self.feerate_label.setText(self.main_window.format_amount(fee_rate))

        # extra fee
        x_fee = run_hook('get_tx_extra_fee', self.wallet, tx)
        if x_fee:
            x_fee_address, x_fee_amount = x_fee
            self.extra_fee_label.setVisible(True)
            self.extra_fee_value.setVisible(True)
            self.extra_fee_value.setText(self.main_window.format_amount_and_units(x_fee_amount))
        amount = tx.output_value() if self.output_value == '!' else self.output_value
        tx_size = tx.estimated_size()
        fee_warning_tuple = self.wallet.get_tx_fee_warning(
            invoice_amt=amount, tx_size=tx_size, fee=fee)
        if fee_warning_tuple:
            allow_send, long_warning, short_warning = fee_warning_tuple
            self.toggle_send_button(allow_send, message=long_warning)
        else:
            self.toggle_send_button(True)

    def _update_amount_label(self):
        pass

class ConfirmTxDialog(TxEditor):
    help_text = ''#_('Set the mining fee of your transaction')

    def __init__(self, *, window: 'ElectrumWindow', make_tx, output_value: Union[int, str], allow_preview=True):

        TxEditor.__init__(
            self,
            window=window,
            make_tx=make_tx,
            output_value=output_value,
            title=_("New Transaction"), # todo: adapt title for channel funding tx, swaps
            allow_preview=allow_preview)

        BlockingWaitingDialog(window, _("Preparing transaction..."), self.update_tx)
        self.update()

    def _update_amount_label(self):
        tx = self.tx
        if self.output_value == '!':
            if tx:
                amount = tx.output_value()
                amount_str = self.main_window.format_amount_and_units(amount)
            else:
                amount_str = "max"
        else:
            amount = self.output_value
            amount_str = self.main_window.format_amount_and_units(amount)
        self.amount_label.setText(amount_str)

    def update_tx(self, *, fallback_to_zero_fee: bool = False):
        fee_estimator = self.get_fee_estimator()
        try:
            self.tx = self.make_tx(fee_estimator)
            self.not_enough_funds = False
            self.no_dynfee_estimates = False
        except NotEnoughFunds:
            self.not_enough_funds = True
            self.tx = None
            if fallback_to_zero_fee:
                try:
                    self.tx = self.make_tx(0)
                except BaseException:
                    return
            else:
                return
        except NoDynamicFeeEstimates:
            self.no_dynfee_estimates = True
            self.tx = None
            try:
                self.tx = self.make_tx(0)
            except NotEnoughFunds:
                self.not_enough_funds = True
                return
            except BaseException:
                return
        except InternalAddressCorruption as e:
            self.tx = None
            self.main_window.show_error(str(e))
            raise
        self.tx.set_rbf(True)

    def have_enough_funds_assuming_zero_fees(self) -> bool:
        # called in send_tab.py
        try:
            tx = self.make_tx(0)
        except NotEnoughFunds:
            return False
        else:
            return True

    def create_grid(self):
        grid = QGridLayout()
        msg = (_('The amount to be received by the recipient.') + ' '
               + _('Fees are paid by the sender.'))
        self.amount_label = QLabel('')
        self.amount_label.setTextInteractionFlags(Qt.TextSelectableByMouse)

        grid.addWidget(HelpLabel(_("Amount to be sent") + ": ", msg), 0, 0)
        grid.addWidget(self.amount_label, 0, 1)

        msg = _('Bitcoin transactions are in general not free. A transaction fee is paid by the sender of the funds.') + '\n\n'\
              + _('The amount of fee can be decided freely by the sender. However, transactions with low fees take more time to be processed.') + '\n\n'\
              + _('A suggested fee is automatically added to this field. You may override it. The suggested fee increases with the size of the transaction.')

        grid.addWidget(HelpLabel(_("Mining Fee") + ": ", msg), 1, 0)
        grid.addLayout(self.fee_hbox, 1, 1, 1, 3)

        grid.addWidget(HelpLabel(_("Fee target") + ": ", self.fee_combo.help_msg), 3, 0)
        grid.addLayout(self.fee_target_hbox, 3, 1, 1, 3)

        grid.setColumnStretch(4, 1)

        # extra fee
        self.extra_fee_label = QLabel(_("Additional fees") + ": ")
        self.extra_fee_label.setVisible(False)
        self.extra_fee_value = QLabel('')
        self.extra_fee_value.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.extra_fee_value.setVisible(False)
        grid.addWidget(self.extra_fee_label, 5, 0)
        grid.addWidget(self.extra_fee_value, 5, 1)

        # locktime editor
        grid.addWidget(self.locktime_label, 6, 0)
        grid.addWidget(self.locktime_e, 6, 1, 1, 2)

        return grid
