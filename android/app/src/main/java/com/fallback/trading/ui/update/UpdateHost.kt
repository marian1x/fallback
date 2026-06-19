package com.fallback.trading.ui.update

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.lifecycle.ViewModel
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewModelScope
import androidx.lifecycle.viewmodel.initializer
import androidx.lifecycle.viewmodel.viewModelFactory
import com.fallback.trading.AppContainer
import com.fallback.trading.data.ReleaseInfo
import com.fallback.trading.data.UpdateManager
import com.fallback.trading.ui.components.Toast
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

sealed interface UpdateUi {
    data object Hidden : UpdateUi
    data object Checking : UpdateUi
    data class Available(val info: ReleaseInfo) : UpdateUi
    data class Downloading(val progress: Float) : UpdateUi
    data object NeedsPermission : UpdateUi
    data object UpToDate : UpdateUi
    data class Failed(val message: String) : UpdateUi
}

class UpdateViewModel(private val manager: UpdateManager) : ViewModel() {
    private val _state = MutableStateFlow<UpdateUi>(UpdateUi.Hidden)
    val state = _state.asStateFlow()

    private var pending: ReleaseInfo? = null

    /** Silent check on launch — only surfaces UI if an update exists. */
    fun checkOnLaunch() = check(manual = false)

    fun check(manual: Boolean) {
        viewModelScope.launch {
            if (manual) _state.value = UpdateUi.Checking
            val info = runCatching { manager.fetchLatest() }.getOrNull()
            _state.value = when {
                info != null && manager.isNewer(info.versionName) -> {
                    pending = info
                    UpdateUi.Available(info)
                }
                manual && info != null -> UpdateUi.UpToDate
                manual -> UpdateUi.Failed("Couldn't check for updates.")
                else -> UpdateUi.Hidden
            }
        }
    }

    fun confirm() {
        val info = pending ?: return
        if (!manager.canInstall()) {
            _state.value = UpdateUi.NeedsPermission
            return
        }
        viewModelScope.launch {
            _state.value = UpdateUi.Downloading(0f)
            try {
                val file = manager.download(info) { p -> _state.value = UpdateUi.Downloading(p) }
                manager.install(file)
                _state.value = UpdateUi.Hidden
            } catch (e: Exception) {
                _state.value = UpdateUi.Failed(e.message ?: "Download failed.")
            }
        }
    }

    fun grantPermission() {
        manager.requestInstallPermission()
        // Re-show the prompt so the user can tap Update again after granting.
        pending?.let { _state.value = UpdateUi.Available(it) }
    }

    fun dismiss() { _state.value = UpdateUi.Hidden }

    companion object {
        fun factory(container: AppContainer) = viewModelFactory {
            initializer { UpdateViewModel(container.updateManager) }
        }
    }
}

@Composable
fun UpdateHost(viewModel: UpdateViewModel) {
    val state by viewModel.state.collectAsStateWithLifecycle()

    when (val s = state) {
        is UpdateUi.Available -> AlertDialog(
            onDismissRequest = viewModel::dismiss,
            title = { Text("Update available") },
            text = {
                Column(Modifier.fillMaxWidth()) {
                    Text(
                        "Version ${s.info.versionName} is ready to install.",
                        style = MaterialTheme.typography.bodyMedium,
                    )
                    if (s.info.notes.isNotBlank()) {
                        Text(
                            s.info.notes.take(600),
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                            modifier = Modifier
                                .padding(top = 12.dp)
                                .verticalScroll(rememberScrollState()),
                        )
                    }
                }
            },
            confirmButton = { TextButton(onClick = viewModel::confirm) { Text("Update") } },
            dismissButton = { TextButton(onClick = viewModel::dismiss) { Text("Later") } },
        )

        is UpdateUi.Downloading -> AlertDialog(
            onDismissRequest = {},
            title = { Text("Downloading update") },
            text = {
                Column(Modifier.fillMaxWidth()) {
                    LinearProgressIndicator(
                        progress = { s.progress },
                        modifier = Modifier.fillMaxWidth().padding(vertical = 12.dp),
                    )
                    Text("${(s.progress * 100).toInt()}%", style = MaterialTheme.typography.bodySmall)
                }
            },
            confirmButton = {},
        )

        UpdateUi.NeedsPermission -> AlertDialog(
            onDismissRequest = viewModel::dismiss,
            title = { Text("Allow installs") },
            text = {
                Text("To install updates, allow Fallback Trading to install unknown apps, then tap Update again.")
            },
            confirmButton = { TextButton(onClick = viewModel::grantPermission) { Text("Open settings") } },
            dismissButton = { TextButton(onClick = viewModel::dismiss) { Text("Cancel") } },
        )

        UpdateUi.Checking -> AlertDialog(
            onDismissRequest = viewModel::dismiss,
            title = { Text("Checking for updates…") },
            text = { LinearProgressIndicator(Modifier.fillMaxWidth().padding(top = 8.dp)) },
            confirmButton = {},
        )

        UpdateUi.UpToDate -> Toast("You're on the latest version.") { viewModel.dismiss() }

        is UpdateUi.Failed -> Toast(s.message) { viewModel.dismiss() }

        UpdateUi.Hidden -> Unit
    }
}
