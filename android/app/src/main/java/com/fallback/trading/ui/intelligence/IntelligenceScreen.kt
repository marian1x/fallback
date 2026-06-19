package com.fallback.trading.ui.intelligence

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.unit.dp
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.lifecycle.viewmodel.initializer
import androidx.lifecycle.viewmodel.viewModelFactory
import com.fallback.trading.AppContainer
import com.fallback.trading.data.AnalysisResponseDto
import com.fallback.trading.data.ApiResult
import com.fallback.trading.data.IntelAnswerDto
import com.fallback.trading.data.TradingRepository
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch

data class IntelState(
    val symbols: String = "",
    val question: String = "",
    val loading: Boolean = false,
    val answer: IntelAnswerDto? = null,
    val analysis: AnalysisResponseDto? = null,
    val error: String? = null,
    val sessionExpired: Boolean = false,
)

class IntelligenceViewModel(private val repo: TradingRepository) : ViewModel() {
    private val _state = MutableStateFlow(IntelState())
    val state = _state.asStateFlow()

    fun onSymbols(value: String) = _state.update { it.copy(symbols = value, error = null) }
    fun onQuestion(value: String) = _state.update { it.copy(question = value, error = null) }

    fun ask() {
        val s = _state.value
        if (s.symbols.isBlank()) { _state.update { it.copy(error = "Add at least one symbol.") }; return }
        if (s.question.isBlank()) { _state.update { it.copy(error = "Type a question.") }; return }
        run {
            _state.update { it.copy(loading = true, error = null, analysis = null) }
            viewModelScope.launch {
                when (val r = repo.ask(s.question.trim(), s.symbols.trim())) {
                    is ApiResult.Success -> _state.update { it.copy(loading = false, answer = r.data) }
                    is ApiResult.Error -> _state.update { it.copy(loading = false, error = r.message) }
                    ApiResult.Unauthorized -> _state.update { it.copy(loading = false, sessionExpired = true) }
                }
            }
        }
    }

    fun showAnalysis() {
        val s = _state.value
        if (s.symbols.isBlank()) { _state.update { it.copy(error = "Add at least one symbol.") }; return }
        _state.update { it.copy(loading = true, error = null, answer = null) }
        viewModelScope.launch {
            when (val r = repo.analysis(s.symbols.trim())) {
                is ApiResult.Success -> _state.update { it.copy(loading = false, analysis = r.data) }
                is ApiResult.Error -> _state.update { it.copy(loading = false, error = r.message) }
                ApiResult.Unauthorized -> _state.update { it.copy(loading = false, sessionExpired = true) }
            }
        }
    }

    companion object {
        fun factory(container: AppContainer) = viewModelFactory {
            initializer { IntelligenceViewModel(container.repository) }
        }
    }
}

@Composable
fun IntelligenceScreen(
    container: AppContainer,
    onSessionExpired: () -> Unit,
    viewModel: IntelligenceViewModel = viewModel(factory = IntelligenceViewModel.factory(container)),
) {
    val state by viewModel.state.collectAsStateWithLifecycle()

    LaunchedEffect(state.sessionExpired) {
        if (state.sessionExpired) onSessionExpired()
    }

    Column(
        modifier = Modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .imePadding()
            .padding(start = 16.dp, end = 16.dp, top = 16.dp, bottom = 96.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Text(
            "Ask the news-aware model about one or more symbols, or pull up the standing analysis the engine keeps per ticker.",
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )

        OutlinedTextField(
            value = state.symbols,
            onValueChange = viewModel::onSymbols,
            label = { Text("Symbols") },
            placeholder = { Text("AAPL, MSFT") },
            singleLine = true,
            keyboardOptions = KeyboardOptions(imeAction = ImeAction.Next),
            modifier = Modifier.fillMaxWidth(),
        )

        OutlinedTextField(
            value = state.question,
            onValueChange = viewModel::onQuestion,
            label = { Text("Question") },
            placeholder = { Text("What's the near-term setup?") },
            minLines = 2,
            keyboardOptions = KeyboardOptions(imeAction = ImeAction.Default),
            modifier = Modifier.fillMaxWidth(),
        )

        Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
            Button(
                onClick = viewModel::ask,
                enabled = !state.loading,
                modifier = Modifier.weight(1f),
            ) { Text("Ask") }
            OutlinedButton(
                onClick = viewModel::showAnalysis,
                enabled = !state.loading,
                modifier = Modifier.weight(1f),
            ) { Text("Show analysis") }
        }

        if (state.loading) {
            Row(
                modifier = Modifier.fillMaxWidth().padding(top = 8.dp),
                horizontalArrangement = Arrangement.Center,
            ) {
                CircularProgressIndicator(modifier = Modifier.size(28.dp), strokeWidth = 2.dp)
            }
        }

        state.error?.let {
            Text(it, color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodyMedium)
        }

        state.answer?.let { AnswerCard(it) }

        state.analysis?.let { analysis ->
            analysis.symbols.forEach { symbol ->
                analysis.results[symbol]?.let { AnalysisCard(symbol, it) }
            }
        }
    }
}

@Composable
private fun AnswerCard(answer: IntelAnswerDto) {
    Card(modifier = Modifier.fillMaxWidth()) {
        Column(Modifier.padding(16.dp)) {
            Text(
                answer.answer.ifBlank { "No answer returned." },
                style = MaterialTheme.typography.bodyLarge,
            )
            val footer = buildList {
                answer.model?.let { add(it) }
                answer.latencySec?.let { add("${it}s") }
            }.joinToString(" • ")
            if (footer.isNotEmpty()) {
                Text(
                    footer,
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier.padding(top = 12.dp),
                )
            }
        }
    }
}

@Composable
private fun AnalysisCard(symbol: String, analysis: com.fallback.trading.data.SymbolAnalysisDto) {
    Card(modifier = Modifier.fillMaxWidth()) {
        Column(Modifier.padding(16.dp)) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
            ) {
                Text(symbol, style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.SemiBold)
                analysis.analystStance?.takeIf { it.isNotBlank() }?.let {
                    Text(it.uppercase(), style = MaterialTheme.typography.labelMedium, color = MaterialTheme.colorScheme.primary)
                }
            }
            if (!analysis.hasAnalysis) {
                Text(
                    "No standing analysis yet for $symbol.",
                    style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier.padding(top = 8.dp),
                )
            } else {
                analysis.narrativeSummary?.takeIf { it.isNotBlank() }?.let {
                    Text(it, style = MaterialTheme.typography.bodyMedium, modifier = Modifier.padding(top = 8.dp))
                }
                if (analysis.recurringThemes.isNotEmpty()) {
                    Text(
                        "Themes: " + analysis.recurringThemes.joinToString(", "),
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                        modifier = Modifier.padding(top = 8.dp),
                    )
                }
                analysis.dossierUpdatedAt?.let {
                    Text(
                        "Updated: ${com.fallback.trading.ui.Format.dateTime(it)}",
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                        modifier = Modifier.padding(top = 8.dp),
                    )
                }
            }
        }
    }
}
