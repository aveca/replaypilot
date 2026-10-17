# Harmless WinForms test app for the ReplayPilot proof.
#
# WinForms controls are real HWNDs, so the Windows accessibility tree (UIA)
# exposes each one by AccessibleName. That is what Lucid's click_element and
# focus_window actions rely on. Tkinter would not work here: Tk paints its own
# widgets into a single window and never exposes them to UIA.
#
# The app does nothing except mirror what is typed into a JSON file, so the
# proof harness can verify a replay by observation. No network, no destructive
# action, no writes outside data/.

param(
    [string]$StatePath = (Join-Path $PSScriptRoot "..\data\testapp_state.json"),
    [string]$EventsPath = (Join-Path $PSScriptRoot "..\data\testapp_events.log")
)

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$ErrorActionPreference = "Stop"

$StatePath = [System.IO.Path]::GetFullPath($StatePath)
$EventsPath = [System.IO.Path]::GetFullPath($EventsPath)
[System.IO.Directory]::CreateDirectory([System.IO.Path]::GetDirectoryName($StatePath)) | Out-Null

$script:Clicks = 0

function Write-State {
    param([string]$Text)
    $obj = [ordered]@{ entry_text = $Text; submit_clicks = $script:Clicks }
    ($obj | ConvertTo-Json -Compress) | Set-Content -Path $StatePath -Encoding UTF8
}

function Write-Event {
    param([string]$Kind, [string]$Detail = "")
    $line = [ordered]@{ event = $Kind; detail = $Detail } | ConvertTo-Json -Compress
    Add-Content -Path $EventsPath -Value $line -Encoding UTF8
}

Write-State ""
Write-Event "start"

$form = New-Object System.Windows.Forms.Form
$form.Text = "ReplayPilot Test App"
$form.Width = 620
$form.Height = 260
$form.StartPosition = "Manual"
$form.Location = New-Object System.Drawing.Point(160, 160)

$label = New-Object System.Windows.Forms.Label
$label.Text = "Type here, then press Submit:"
$label.AutoSize = $true
$label.Location = New-Object System.Drawing.Point(24, 26)
$label.Font = New-Object System.Drawing.Font("Segoe UI", 11)
$form.Controls.Add($label)

$box = New-Object System.Windows.Forms.TextBox
$box.Name = "TaskField"
$box.AccessibleName = "TaskField"
$box.Width = 520
$box.Location = New-Object System.Drawing.Point(24, 62)
$box.Font = New-Object System.Drawing.Font("Consolas", 13)
$form.Controls.Add($box)

$button = New-Object System.Windows.Forms.Button
$button.Name = "SubmitButton"
$button.AccessibleName = "Submit"
$button.Text = "Submit"
$button.Width = 140
$button.Height = 36
$button.Location = New-Object System.Drawing.Point(24, 106)
$button.Font = New-Object System.Drawing.Font("Segoe UI", 10)
$form.Controls.Add($button)

$status = New-Object System.Windows.Forms.Label
$status.AccessibleName = "Status"
$status.Text = "submits: 0"
$status.AutoSize = $true
$status.Location = New-Object System.Drawing.Point(24, 156)
$form.Controls.Add($status)

$box.Add_TextChanged({
    Write-State -Text $box.Text
})

$button.Add_Click({
    $script:Clicks++
    Write-Event -Kind "submit" -Detail $box.Text
    Write-State -Text $box.Text
    $status.Text = "submits: $($script:Clicks)"
})

# Keep the caret in the field once the window is up, so a replay that only
# focuses the window behaves the same as one that also clicks the field.
$form.Add_Shown({
    $form.Activate()
    $box.Focus()
})

[System.Windows.Forms.Application]::Run($form)