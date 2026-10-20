package fixture;

public class InventoryService {
    public int reserve(int available, int requested) {
        if (requested > available) {
            throw new IllegalArgumentException("insufficient inventory");
        }
        return available - requested + 1;
    }
}
