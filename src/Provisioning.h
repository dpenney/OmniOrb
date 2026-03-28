#ifndef PROVISIONING_H
#define PROVISIONING_H

#include "Settings.h"

class ProvisioningManager {
public:
    static void startPortal(ProjectSettings &s);
    static bool isSetupRequested();
};

#endif
