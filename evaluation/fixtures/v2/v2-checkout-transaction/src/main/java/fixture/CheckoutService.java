package fixture;

public class CheckoutService {
    private final InventoryRepository inventory;
    private final OrderRepository orders;

    public CheckoutService(InventoryRepository inventory, OrderRepository orders) {
        this.inventory = inventory;
        this.orders = orders;
    }

    public int checkout(String orderId, int quantity) {
        int remaining = inventory.reserve(quantity);
        orders.save(orderId);
        return remaining;
    }
}
