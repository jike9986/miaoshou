[Code]
procedure BrowseWithNewFolder(Sender: TObject);
var
  Selected: String;
begin
  Selected := WizardForm.DirEdit.Text;
  if BrowseForFolder('选择安装位置', Selected, True) then
    WizardForm.DirEdit.Text := Selected;
end;

procedure InitializeWizard();
begin
  WizardForm.DirBrowseButton.OnClick := @BrowseWithNewFolder;
end;
