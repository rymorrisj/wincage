// Tests Qt 5.15 QPA platform plugin load and window display inside an AppContainer.
// Exits 0 on PASS, 1 on FAIL. Prints one status line to stdout.
#include <QApplication>
#include <QMessageBox>
#include <QTimer>
#include <iostream>
#include <string>

int main() {
    int argc = 0;
    QApplication app(argc, nullptr);

    std::string platform = app.platformName().toStdString();
    if (platform.empty()) {
        std::cout << "FAIL: Qt QPA platform plugin not loaded (platformName is empty)\n";
        return 1;
    }

    QMessageBox box;
    box.setText("wincage Qt QPA capability check, closing automatically.");
    QTimer::singleShot(1000, &box, &QMessageBox::accept);
    box.exec();

    std::cout << "PASS: Qt QPA plugin loaded (platform=" << platform
              << ") and QMessageBox displayed\n";
    return 0;
}
