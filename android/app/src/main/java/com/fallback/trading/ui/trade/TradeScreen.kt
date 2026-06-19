package com.fallback.trading.ui.trade

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AssistChip
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.ExposedDropdownMenuBox
import androidx.compose.material3.ExposedDropdownMenuDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.MenuAnchorType
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.SegmentedButton
import androidx.compose.material3.SegmentedButtonDefaults
import androidx.compose.material3.SingleChoiceSegmentedButtonRow
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardCapitalization
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.initializer
import androidx.lifecycle.viewmodel.viewModelFactory
import com.fallback.trading.AppContainer
import com.fallback.trading.data.ApiResult
import com.fallback.trading.data.SymbolDto
import com.fallback.trading.data.TradeRequest
import com.fallback.trading.data.TradingRepository
import com.fallback.trading.ui.components.Toast
import com.fallback.trading.ui.theme.LossRed
import com.fallback.trading.ui.theme.ProfitGreen
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch

private val TIME_IN_FORCE = listOf("day", "gtc", "ioc", "fok", "opg", "cls")

data class TradeForm(
    val symbol: String = "",
    val action: String = "buy",
    val amount: String = "",
    val orderType: String = "market",
    val limitPrice: String = "",
    val timeInForce: String = "day",
    val extendedHours: Boolean = false,
    val submitting: Boolean = false,
    val symbolError: String? = null,
    val sessionExpired: Boolean = false,
)

class TradeViewModel(private val repo: TradingRepository) : ViewModel() {
    private val _form = MutableStateFlow(TradeForm())
    val form = _form.asStateFlow()

    private val _symbols = MutableStateFlow<List<SymbolDto>>(emptyList())
    private val _message = MutableStateFlow<String?>(null)
    val message = _message.asStateFlow()

    init {
        viewModelScope.launch {
            (repo.getTradableSymbols() as? ApiResult.Success)?.let { _symbols.value = it.data }
        }
    }

    fun update(transform: (TradeForm) -> TradeForm) = _form.update(transform)

    fun setSymbol(symbol: String) = _form.update { it.copy(symbol = symbol.uppercase(), symbolError = null) }

    fun suggestions(prefix: String): List<String> {
        val p = prefix.trim().uppercase()
        if (p.isEmpty()) return emptyList()
        val all = _symbols.value
        if (all.isEmpty() || all.any { it.symbol == p }) return emptyList()
        return all.asSequence().filter { it.symbol.startsWith(p) }.take(8).map { it.symbol }.toList()
    }

    fun submit(onSuccess: () -> Unit = {}) {
        val f = _form.value
        val symbol = f.symbol.trim().uppercase()
        val known = _symbols.value
        if (symbol.isEmpty()) {
            _form.update { it.copy(symbolError = "Enter a symbol.") }
            return
        }
        if (known.isNotEmpty() && known.none { it.symbol == symbol }) {
            _form.update { it.copy(symbolError = "“$symbol” is not in your tradable list.") }
            return
        }
        val amount = f.amount.trim().toDoubleOrNull()
        if (f.amount.isNotBlank() && (amount == null || amount <= 0)) {
            _message.value = "Amount must be a positive number."
            return
        }
        val limit = f.limitPrice.trim().toDoubleOrNull()
        if (f.orderType == "limit" && (limit == null || limit <= 0)) {
            _message.value = "Enter a valid limit price."
            return
        }

        val request = TradeRequest(
            symbol = symbol,
            action = f.action,
            amount = amount,
            orderType = f.orderType,
            timeInForce = f.timeInForce,
            extendedHours = f.extendedHours,
            limitPrice = if (f.orderType == "limit") limit else null,
        )

        viewModelScope.launch {
            _form.update { it.copy(submitting = true, symbolError = null) }
            when (val r = repo.placeTrade(request)) {
                is ApiResult.Success -> {
                    val extra = r.data.result ?: r.data.code ?: r.data.status
                    _message.value = buildString {
                        append("${f.action.uppercase()} order sent for $symbol")
                        if (!extra.isNullOrBlank()) append(" • $extra")
                    }
                    _form.update { it.copy(submitting = false) }
                    onSuccess()
                }
                is ApiResult.Error -> {
                    _message.value = "Order failed: ${r.message}"
                    _form.update { it.copy(submitting = false) }
                }
                ApiResult.Unauthorized -> _form.update { it.copy(submitting = false, sessionExpired = true) }
            }
        }
    }

    fun consumeMessage() { _message.value = null }

    companion object {
        fun factory(container: AppContainer) = viewModelFactory {
            initializer { TradeViewModel(container.repository) }
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun TradeSheet(
    viewModel: TradeViewModel,
    onSessionExpired: () -> Unit,
    onClose: () -> Unit,
) {
    val form by viewModel.form.collectAsStateWithLifecycle()
    val message by viewModel.message.collectAsStateWithLifecycle()
    val actionColor = if (form.action == "buy") ProfitGreen else LossRed

    LaunchedEffect(form.sessionExpired) {
        if (form.sessionExpired) onSessionExpired()
    }
    message?.let { Toast(it) { viewModel.consumeMessage() } }

    Column(
        modifier = Modifier
            .fillMaxWidth()
            .verticalScroll(rememberScrollState())
            .imePadding()
            .navigationBarsPadding()
            .padding(horizontal = 20.dp)
            .padding(bottom = 20.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp),
    ) {
        Text("New order", style = MaterialTheme.typography.titleLarge, fontWeight = FontWeight.Bold)

        BuySellToggle(action = form.action) { viewModel.update { f -> f.copy(action = it) } }

        OutlinedTextField(
            value = form.symbol,
            onValueChange = { viewModel.update { s -> s.copy(symbol = it.uppercase(), symbolError = null) } },
            label = { Text("Symbol") },
            placeholder = { Text("AAPL, BTCUSD…") },
            singleLine = true,
            isError = form.symbolError != null,
            supportingText = form.symbolError?.let { { Text(it) } },
            keyboardOptions = KeyboardOptions(
                capitalization = KeyboardCapitalization.Characters,
                imeAction = ImeAction.Next,
            ),
            modifier = Modifier.fillMaxWidth(),
        )
        val suggestions = viewModel.suggestions(form.symbol)
        if (suggestions.isNotEmpty()) {
            LazyRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                items(suggestions) { suggestion ->
                    AssistChip(
                        onClick = { viewModel.setSymbol(suggestion) },
                        label = { Text(suggestion) },
                    )
                }
            }
        }

        OutlinedTextField(
            value = form.amount,
            onValueChange = { viewModel.update { s -> s.copy(amount = it) } },
            label = { Text("Amount (USD)") },
            placeholder = { Text("Defaults to your per-trade amount") },
            singleLine = true,
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Decimal, imeAction = ImeAction.Next),
            modifier = Modifier.fillMaxWidth(),
        )

        Text("Order type", style = MaterialTheme.typography.labelLarge)
        SingleChoiceSegmentedButtonRow(modifier = Modifier.fillMaxWidth()) {
            listOf("market" to "Market", "limit" to "Limit").forEachIndexed { index, (value, label) ->
                SegmentedButton(
                    selected = form.orderType == value,
                    onClick = { viewModel.update { it.copy(orderType = value) } },
                    shape = SegmentedButtonDefaults.itemShape(index, 2),
                ) { Text(label) }
            }
        }

        if (form.orderType == "limit") {
            OutlinedTextField(
                value = form.limitPrice,
                onValueChange = { viewModel.update { s -> s.copy(limitPrice = it) } },
                label = { Text("Limit price") },
                singleLine = true,
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Decimal, imeAction = ImeAction.Done),
                modifier = Modifier.fillMaxWidth(),
            )
        }

        TimeInForceDropdown(
            selected = form.timeInForce,
            onSelected = { viewModel.update { f -> f.copy(timeInForce = it) } },
        )

        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Column(Modifier.weight(1f)) {
                Text("Extended hours", style = MaterialTheme.typography.bodyLarge)
                Text(
                    "Pre/post-market (limit + DAY/GTC).",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
            Switch(
                checked = form.extendedHours,
                onCheckedChange = { viewModel.update { f -> f.copy(extendedHours = it) } },
            )
        }

        Button(
            onClick = { viewModel.submit(onSuccess = onClose) },
            enabled = !form.submitting,
            colors = ButtonDefaults.buttonColors(containerColor = actionColor, contentColor = Color.White),
            modifier = Modifier.fillMaxWidth().padding(top = 4.dp),
        ) {
            if (form.submitting) {
                CircularProgressIndicator(
                    modifier = Modifier.size(18.dp).padding(end = 8.dp),
                    strokeWidth = 2.dp,
                    color = Color.White,
                )
            }
            Text(
                if (form.submitting) "Sending…"
                else "${form.action.uppercase()} ${form.symbol.ifBlank { "order" }}",
                fontWeight = FontWeight.Bold,
            )
        }
    }
}

@Composable
private fun BuySellToggle(action: String, onChange: (String) -> Unit) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(14.dp))
            .background(MaterialTheme.colorScheme.surfaceVariant)
            .padding(4.dp),
        horizontalArrangement = Arrangement.spacedBy(4.dp),
    ) {
        ToggleHalf("Buy", action == "buy", ProfitGreen, Modifier.weight(1f)) { onChange("buy") }
        ToggleHalf("Sell", action == "sell", LossRed, Modifier.weight(1f)) { onChange("sell") }
    }
}

@Composable
private fun ToggleHalf(
    text: String,
    selected: Boolean,
    color: Color,
    modifier: Modifier = Modifier,
    onClick: () -> Unit,
) {
    Box(
        modifier = modifier
            .clip(RoundedCornerShape(10.dp))
            .background(if (selected) color else Color.Transparent)
            .clickable(onClick = onClick)
            .padding(vertical = 12.dp),
        contentAlignment = Alignment.Center,
    ) {
        Text(
            text,
            color = if (selected) Color.White else MaterialTheme.colorScheme.onSurfaceVariant,
            fontWeight = FontWeight.Bold,
        )
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun TimeInForceDropdown(selected: String, onSelected: (String) -> Unit) {
    var expanded by remember { mutableStateOf(false) }
    ExposedDropdownMenuBox(expanded = expanded, onExpandedChange = { expanded = it }) {
        OutlinedTextField(
            value = selected.uppercase(),
            onValueChange = {},
            readOnly = true,
            label = { Text("Time in force") },
            trailingIcon = { ExposedDropdownMenuDefaults.TrailingIcon(expanded = expanded) },
            modifier = Modifier
                .fillMaxWidth()
                .menuAnchor(MenuAnchorType.PrimaryNotEditable),
        )
        ExposedDropdownMenu(expanded = expanded, onDismissRequest = { expanded = false }) {
            TIME_IN_FORCE.forEach { tif ->
                DropdownMenuItem(
                    text = { Text(tif.uppercase()) },
                    onClick = {
                        onSelected(tif)
                        expanded = false
                    },
                )
            }
        }
    }
}
